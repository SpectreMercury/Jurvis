"""The user's spending ledger: what they spent, where it came from, and how it adds up. Bookkeeping, not investing.

Privacy is a property of this module's shape, not of a prompt:
- Single transactions (merchant, note, time) stay on this Mac. They are shown by the UI straight from the database
  and are handed only to the local model (transactions(), classify()).
- What any model, cloud ones included, may see is summary(): totals and counts by category and period, never a merchant.
- Every figure is added up here in integer cents. The model quotes results; it never does the arithmetic.
"""
import csv
import hashlib
import io
import json
import re
import time
from datetime import date, datetime, timedelta

from .store import new_id

CATEGORIES = ["餐饮", "交通", "购物", "日用", "住房", "娱乐", "医疗", "教育", "通讯", "旅行", "订阅", "转账", "其他"]
TOTAL = "总计"
# Alipay's own categories, folded into ours.
ALIPAY = {"餐饮美食": "餐饮", "交通出行": "交通", "日用百货": "日用", "服饰装扮": "购物", "数码电器": "购物", "美容美发": "购物", "母婴亲子": "购物", "家居家装": "日用",
          "文化休闲": "娱乐", "运动户外": "娱乐", "宠物": "日用", "医疗健康": "医疗", "教育培训": "教育", "充值缴费": "通讯", "住房物业": "住房", "酒店旅游": "旅行",
          "爱车养车": "交通", "转账红包": "转账", "亲友代付": "转账", "生活服务": "日用", "公共服务": "日用", "商业服务": "其他", "保险": "其他", "公益捐赠": "其他"}
# What a merchant or item name says about the category, for bills that carry none (WeChat Pay). First match wins.
KEYWORDS = [("订阅", r"会员|订阅|icloud|apple\.com/bill|netflix|spotify|chatgpt|openai|claude|youtube premium|网易云|qq音乐|爱奇艺|腾讯视频|优酷|哔哩哔哩大会员"),
            ("餐饮", r"美团|饿了么|餐|饭|面|粉|咖啡|coffee|奶茶|茶饮|瑞幸|星巴克|麦当劳|肯德基|汉堡|火锅|烧烤|小吃|食堂|外卖|蛋糕|面包|饮品|酒吧|便当|寿司|披萨"),
            ("交通", r"滴滴|打车|出租|地铁|公交|铁路|12306|高铁|航空|机票|加油|石化|石油|停车|etc|高速|单车|哈啰|曹操|t3出行|花小猪"),
            ("旅行", r"酒店|民宿|携程|去哪儿|飞猪|booking|airbnb|景区|门票"),
            ("住房", r"房租|物业|水费|电费|燃气|自如|贝壳|链家|宽带"),
            ("通讯", r"话费|流量|移动|联通|电信"),
            ("医疗", r"医院|药房|药店|诊所|门诊|体检|挂号"),
            ("教育", r"学费|培训|课程|书店|图书|得到|极客时间"),
            ("娱乐", r"电影|影城|游戏|steam|ktv|演出|健身|游泳|球馆|剧本杀"),
            ("日用", r"超市|便利店|全家|罗森|7-eleven|盒马|永辉|沃尔玛|山姆|洗衣|理发|快递|菜鸟|水果|生鲜|菜市场"),
            ("购物", r"京东|淘宝|天猫|拼多多|抖音电商|唯品会|小米|apple store|优衣库|无印良品|宜家|得物"),
            ("转账", r"转账|红包|还款|信用卡")]
CLASSIFY = """You sort a person's payments into spending categories for their private ledger. For each numbered line (who was paid, and what for when known), choose exactly one category from this list:
{categories}
Use 其他 only when nothing else fits. Reply with JSON only: an object from the line number to the category, for example {{"1": "餐饮", "2": "交通"}}."""


def cents(amount):
    return int(round(float(amount) * 100))


def yuan(value):
    """12345 cents -> "¥123.45"; whole amounts drop the decimals."""
    text = f"{abs(value) / 100:,.2f}"
    return ("-" if value < 0 else "") + "¥" + (text[:-3] if text.endswith(".00") else text)


def guess(text):
    """The category a merchant or item name points to, or 其他."""
    low = text.casefold()
    for category, pattern in KEYWORDS:
        if re.search(pattern, low):
            return category
    return "其他"


def span(period, today=None):
    """(first day, day after the last, label) for today, this_week, this_month, last_month, this_year, YYYY-MM or YYYY."""
    today = today or date.today()
    period = (period or "this_month").strip()
    month = re.fullmatch(r"(\d{4})-(\d{1,2})", period)
    if period == "today":
        return today, today + timedelta(days=1), "今天"
    if period == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7), "本周"
    if period == "last_month":
        end = today.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
        return start, end, f"{start.month} 月"
    if period == "this_year" or re.fullmatch(r"\d{4}", period):
        year = today.year if period == "this_year" else int(period)
        return date(year, 1, 1), date(year + 1, 1, 1), f"{year} 年"
    if month and 1 <= int(month.group(2)) <= 12:
        start = date(int(month.group(1)), int(month.group(2)), 1)
    elif period == "this_month":
        start = today.replace(day=1)
    else:
        raise ValueError("period must be today, this_week, this_month, last_month, this_year, YYYY-MM or YYYY")
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end, f"{start.month} 月" if start.year == today.year else f"{start.year} 年 {start.month} 月"


def stamp(day):
    return datetime(day.year, day.month, day.day).timestamp()


class Ledger:
    def __init__(self, store):
        self.store = store

    # ---- writing ----
    def add(self, amount, category="其他", merchant="", note="", when=None, income=False, source="chat", ref=None, auto=False):
        value = cents(amount)
        if value <= 0:
            raise ValueError("amount must be greater than zero")
        if value > 10 ** 11:
            raise ValueError("amount is too large")
        if category not in CATEGORIES:
            raise ValueError("category must be one of " + ", ".join(CATEGORIES))
        ident, now = new_id(), time.time()
        self.store.execute("INSERT INTO transactions(id,at,amount_cents,direction,category,merchant,note,source,ref,auto,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (ident, when or now, value, "income" if income else "expense", category, merchant.strip()[:80], note.strip()[:200], source, ref, 1 if auto else 0, now))
        self.store.event("ledger", ident)
        return self.store.one("SELECT * FROM transactions WHERE id=?", (ident,))

    def remove(self, ident):
        self.store.execute("DELETE FROM transactions WHERE id=?", (ident,))
        self.store.event("ledger", ident)

    def recategorize(self, ident, category):
        if category not in CATEGORIES:
            raise ValueError("unknown category")
        self.store.execute("UPDATE transactions SET category=?,auto=0 WHERE id=?", (category, ident))
        self.store.event("ledger", ident)

    def set_budget(self, category, amount):
        if category != TOTAL and category not in CATEGORIES:
            raise ValueError(f"category must be {TOTAL} or one of " + ", ".join(CATEGORIES))
        if cents(amount) <= 0:
            self.store.execute("DELETE FROM budgets WHERE category=?", (category,))
        else:
            self.store.execute("INSERT INTO budgets(category,amount_cents,updated_at) VALUES(?,?,?) ON CONFLICT(category) DO UPDATE SET amount_cents=excluded.amount_cents,updated_at=excluded.updated_at",
                               (category, cents(amount), time.time()))
        self.store.event("ledger", category)

    def budgets(self):
        return {row["category"]: row["amount_cents"] for row in self.store.all("SELECT * FROM budgets")}

    # ---- bills ----
    def import_bill(self, data, filename=""):
        """Reads an Alipay or WeChat Pay bill export (CSV). Returns how many payments were added and how many were already there."""
        text = None
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("读不出这个文件的文字编码，请导出为 CSV 再试")
        rows = list(csv.reader(io.StringIO(text)))
        # Both exports open with a few lines about the account; the table starts at the row that names its columns.
        head = next((n for n, row in enumerate(rows) if any("交易时间" in cell for cell in row) and any("金额" in cell for cell in row)), None)
        if head is None:
            raise ValueError("没有找到账单表头（交易时间、金额）。目前支持支付宝和微信支付导出的 CSV 账单")
        columns = [cell.strip() for cell in rows[head]]

        def column(*names):
            return next((n for n, cell in enumerate(columns) if any(cell.startswith(name) for name in names)), None)
        at, amount, way, who, what = column("交易时间"), column("金额"), column("收/支"), column("交易对方"), column("商品")
        kind, order, state, remark = column("交易分类"), column("交易单号", "交易订单号"), column("当前状态", "交易状态"), column("备注")
        source = "alipay" if kind is not None else "wechat"
        if None in (at, amount, way):
            raise ValueError("账单缺少必要的列（交易时间、金额、收/支）")
        added = skipped = ignored = 0
        for row in rows[head + 1:]:
            cell = lambda n: row[n].strip() if n is not None and n < len(row) else ""
            direction = {"支出": "expense", "收入": "income"}.get(cell(way))
            try:
                when = datetime.strptime(cell(at)[:19], "%Y-%m-%d %H:%M:%S").timestamp()
                value = float(cell(amount).replace("¥", "").replace("￥", "").replace(",", ""))
            except ValueError:
                continue  # a blank or summary line
            if not direction or value <= 0 or re.search("退款|关闭|失败", cell(state)):
                ignored += 1  # neither spending nor income (moved between own accounts, refunded, closed)
                continue
            merchant, item = cell(who), cell(what)
            ref = source + ":" + (cell(order) or hashlib.sha256(f"{cell(at)}|{value}|{merchant}|{item}".encode()).hexdigest()[:24])
            if self.store.one("SELECT id FROM transactions WHERE ref=?", (ref,)):
                skipped += 1
                continue
            category = ALIPAY.get(cell(kind)) or guess(merchant + " " + item)
            note = item if item not in ("/", merchant) else ""
            if cell(remark) not in ("", "/"):
                note = (note + " " + cell(remark)).strip()
            self.add(value, category, merchant, note, when, income=direction == "income", source=source, ref=ref, auto=True)
            added += 1
        if not added and not skipped:
            raise ValueError("账单里没有可记入的收支记录")
        return {"added": added, "already_there": skipped, "ignored": ignored, "source": {"alipay": "支付宝", "wechat": "微信支付"}[source],
                "uncategorized": len(self.unsorted())}

    def unsorted(self, limit=60):
        """Imported payments still in 其他, one line per payee: what the local model is asked to sort."""
        return self.store.all("SELECT merchant,MAX(note) AS note,COUNT(*) AS count FROM transactions WHERE category='其他' AND auto=1 AND direction='expense' AND merchant!='' "
                              "GROUP BY merchant ORDER BY count DESC,merchant LIMIT ?", (limit,))

    async def classify(self, runtime):
        """Sorts imported payments the keyword rules could not place. `runtime` is the local model: payee names never go to a cloud model."""
        pending = self.unsorted()
        if not pending:
            return {"classified": 0}
        lines = "\n".join(f"{n}. {row['merchant']}" + (f"（{row['note'][:40]}）" if row["note"] else "") for n, row in enumerate(pending, 1))
        reply = await runtime.complete([{"role": "system", "content": CLASSIFY.format(categories="、".join(CATEGORIES))}, {"role": "user", "content": lines}],
                                       max_tokens=1500, temperature=0)
        match = re.search(r"\{.*\}", reply, re.S)
        try:
            chosen = json.loads(match.group(0)) if match else {}
        except json.JSONDecodeError:
            chosen = {}
        done = 0
        for n, row in enumerate(pending, 1):
            category = chosen.get(str(n))
            if category in CATEGORIES and category != "其他":
                self.store.execute("UPDATE transactions SET category=? WHERE category='其他' AND auto=1 AND merchant=?", (category, row["merchant"]))
                done += 1
        # Whatever stays in 其他 was looked at: it is not offered to the model again.
        self.store.execute("UPDATE transactions SET auto=0 WHERE category='其他' AND auto=1")
        self.store.event("ledger", "classified")
        return {"classified": done, "payees": len(pending)}

    # ---- reading ----
    def summary(self, period="this_month", category="", today=None):
        """Totals for a period: no merchants, no single payments. This is all a cloud model ever sees of the ledger."""
        today = today or date.today()
        start, end, label = span(period, today)
        if category and category not in CATEGORIES:
            raise ValueError("category must be one of " + ", ".join(CATEGORIES))
        where = "at>=? AND at<? AND direction=?" + (" AND category=?" if category else "")

        def total(direction, first=start, last=end):
            row = self.store.one(f"SELECT COALESCE(SUM(amount_cents),0) AS total,COUNT(*) AS count FROM transactions WHERE {where}",
                                 (stamp(first), stamp(last), direction, *([category] if category else [])))
            return row["total"], row["count"]
        spent, count = total("expense")
        income, _ = total("income")
        groups = self.store.all(f"SELECT category,SUM(amount_cents) AS total,COUNT(*) AS count FROM transactions WHERE {where} GROUP BY category ORDER BY total DESC",
                                (stamp(start), stamp(end), "expense", *([category] if category else [])))
        # The same stretch one period earlier, for "more or less than before".
        length = end - start
        before_start = (start - timedelta(days=1)).replace(day=1) if length.days >= 28 and start.day == 1 and length.days < 40 else start - length
        before, _ = total("expense", before_start, start)
        days = max(1, (min(end, today + timedelta(days=1)) - start).days)
        budgets = self.budgets()
        limit = budgets.get(category or TOTAL)
        monthly = start.day == 1 and 28 <= length.days <= 31
        result = {"period": label, "from": start.isoformat(), "to": (end - timedelta(days=1)).isoformat(), "currency": "CNY",
                  "spent": spent / 100, "payments": count, "income": income / 100, "per_day": round(spent / days / 100, 2),
                  "previous_period_spent": before / 100, "change_percent": round((spent - before) / before * 100, 1) if before else None,
                  "by_category": [{"category": g["category"], "spent": g["total"] / 100, "payments": g["count"], "share_percent": round(g["total"] / spent * 100, 1) if spent else 0}
                                  for g in groups]}
        if category:
            result["category"] = category
        if limit and monthly:
            result["budget"] = {"limit": limit / 100, "used_percent": round(spent / limit * 100, 1), "left": (limit - spent) / 100}
        if monthly:
            over = [{"category": g["category"], "limit": budgets[g["category"]] / 100, "spent": g["total"] / 100} for g in groups
                    if g["category"] in budgets and g["total"] > budgets[g["category"]] and not category]
            if over:
                result["over_budget"] = over
        rows = [{"label": "笔数", "value": f"{count} 笔"}, {"label": "日均", "value": yuan(round(spent / days))}]
        if before:
            rows.append({"label": "较上期", "value": f"{(spent - before) / before * 100:+.1f}%"})
        if income:
            rows.append({"label": "收入", "value": yuan(income)})
        if "budget" in result:
            rows.append({"label": "预算", "value": f"{yuan(limit)} · 已用 {result['budget']['used_percent']:g}%"})
        result["card"] = {"kind": "ledger", "title": f"{label}{category}支出", "subtitle": f"{start:%m/%d} – {end - timedelta(days=1):%m/%d}", "value": yuan(spent), "icon": "wallet",
                          "bars": [{"label": g["category"], "value": yuan(g["total"]), "share": round(g["total"] / spent, 4) if spent else 0} for g in groups[:6]] if not category else [],
                          "rows": rows, "note": "每一笔明细在「账本」里，只存在这台 Mac 上", "period": period}
        return result

    def transactions(self, period="this_month", category="", merchant="", limit=200, today=None):
        """Single payments, newest first. For the UI and the local model only."""
        start, end, label = span(period, today)
        where, params = "at>=? AND at<?", [stamp(start), stamp(end)]
        if category:
            where, params = where + " AND category=?", params + [category]
        if merchant:
            where, params = where + " AND merchant LIKE ?", params + [f"%{merchant}%"]
        rows = self.store.all(f"SELECT * FROM transactions WHERE {where} ORDER BY at DESC LIMIT ?", (*params, limit))
        return {"period": label, "transactions": rows}

    def recorded(self, row, today=None):
        """What logging one payment gives back: the entry, where the month now stands, and its card."""
        month = self.summary("this_month", today=today)
        same = next((g for g in month["by_category"] if g["category"] == row["category"]), {"spent": 0})
        income = row["direction"] == "income"
        card = {"kind": "expense", "title": row["merchant"] or row["note"] or row["category"], "subtitle": ("已记一笔收入" if income else "已记一笔") + " · " + row["category"],
                "value": ("+" if income else "") + yuan(row["amount_cents"]), "icon": "wallet",
                "rows": [{"label": f"本月{row['category']}", "value": yuan(cents(same["spent"]))}, {"label": "本月合计", "value": yuan(cents(month["spent"]))}],
                "note": datetime.fromtimestamp(row["at"]).strftime("%m月%d日 %H:%M")}
        result = {"recorded": {"amount": row["amount_cents"] / 100, "category": row["category"], "direction": row["direction"]},
                  "month_spent": month["spent"], "month_spent_in_category": same["spent"], "card": card}
        if "budget" in month:
            result["budget"] = month["budget"]
        return result
