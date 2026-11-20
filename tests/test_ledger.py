"""The spending ledger: logging, bill import, totals, budgets, and what a cloud model may and may not see of it."""
import json
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.config import Settings
from engine.ledger import guess, span, yuan
from engine.runtime import Completion
from tests.test_agent import ScriptedModel, call, run

ALIPAY = """------------------------------------------------------------------------------------
导出信息：
姓名：测试用户
支付宝账户：test
------------------------支付宝（中国）网络技术有限公司  电子客户回单------------------------
交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注,
2026-11-03 12:10:00,餐饮美食,兰州拉面,a***@x.com,牛肉面,支出,28.00,余额宝,交易成功,A001,M001,,
2026-11-04 08:30:00,交通出行,滴滴出行,d***@x.com,快车,支出,35.50,花呗,交易成功,A002,M002,,
2026-11-05 20:00:00,转账红包,张三,z***@x.com,转账,不计收支,500.00,余额,交易成功,A003,M003,,
2026-11-06 09:00:00,数码电器,苹果官方旗舰店,p***@x.com,耳机,支出,1299.00,花呗,退款成功,A004,M004,,
2026-11-10 10:00:00,其他,公司,c***@x.com,工资,收入,20000.00,余额,交易成功,A005,M005,,
"""
WECHAT = """微信支付账单明细,,,,,,,,,,
微信昵称：[测试],,,,,,,,,,
----------------------微信支付账单明细列表--------------------,,,,,,,,,,
交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号,商户单号,备注
2026-11-07 19:20:11,商户消费,瑞幸咖啡,生椰拿铁,支出,¥16.00,零钱,支付成功,W001,S001,/
2026-11-08 13:05:40,商户消费,老王修车铺,/,支出,¥300.00,零钱,支付成功,W002,S002,/
2026-11-09 21:00:00,商户消费,阿强的店,扫码付款,支出,"¥1,234.50",银行卡,支付成功,W003,S003,/
"""


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data=tmp_path / "data"), run_worker=False)


def at(day, hour=12):
    return datetime(2026, 11, day, hour).timestamp()


def test_periods_money_and_keyword_categories():
    today = date(2026, 11, 20)
    assert span("this_month", today)[:2] == (date(2026, 11, 1), date(2026, 12, 1)) and span("last_month", today)[2] == "10 月"
    assert span("this_week", today)[:2] == (date(2026, 11, 16), date(2026, 11, 23)) and span("2026-02", today)[1] == date(2026, 3, 1)
    assert span("2025", today)[:2] == (date(2025, 1, 1), date(2026, 1, 1))
    with pytest.raises(ValueError):
        span("上个月", today)
    assert (yuan(3500), yuan(123456), yuan(99)) == ("¥35", "¥1,234.56", "¥0.99")
    assert (guess("瑞幸咖啡 生椰拿铁"), guess("滴滴出行"), guess("Netflix 会员"), guess("阿强的店")) == ("餐饮", "交通", "订阅", "其他")


def test_totals_are_added_up_in_cents_with_budget_and_the_period_before(app):
    ledger = app.state.worker.tools.ledger
    for amount, category, day in ((0.1, "餐饮", 3), (0.2, "餐饮", 4), (35.5, "交通", 5), (64.2, "购物", 6)):
        ledger.add(amount, category, "某店", when=at(day))
    ledger.add(50, "餐饮", when=datetime(2026, 10, 15).timestamp())
    ledger.add(8000, "其他", income=True, when=at(10))
    ledger.set_budget("总计", 200)
    ledger.set_budget("餐饮", 0.2)
    month = ledger.summary("this_month", today=date(2026, 11, 20))
    assert month["spent"] == 100.0 and month["payments"] == 4 and month["income"] == 8000.0  # 0.1 + 0.2 is 0.3, not 0.30000000000000004
    assert [(g["category"], g["spent"], g["share_percent"]) for g in month["by_category"]] == [("购物", 64.2, 64.2), ("交通", 35.5, 35.5), ("餐饮", 0.3, 0.3)]
    assert month["previous_period_spent"] == 50.0 and month["change_percent"] == 100.0 and month["per_day"] == 5.0
    assert month["budget"] == {"limit": 200.0, "used_percent": 50.0, "left": 100.0} and month["over_budget"] == [{"category": "餐饮", "limit": 0.2, "spent": 0.3}]
    card = month["card"]
    assert (card["kind"], card["title"], card["value"]) == ("ledger", "11 月支出", "¥100") and card["bars"][0] == {"label": "购物", "value": "¥64.20", "share": 0.642}
    assert "某店" not in json.dumps(month, ensure_ascii=False)  # no payee in anything a model is given
    assert ledger.summary("this_month", "餐饮", today=date(2026, 11, 20))["spent"] == 0.3


def test_bills_from_alipay_and_wechat_are_read_once_and_only_real_spending_counts(app):
    ledger = app.state.worker.tools.ledger
    first = ledger.import_bill(ALIPAY.encode("gb18030"), "alipay.csv")
    assert (first["added"], first["ignored"], first["source"]) == (3, 2, "支付宝")  # a transfer between own accounts and a refund are left out
    assert ledger.import_bill(ALIPAY.encode("gb18030"))["already_there"] == 3
    second = ledger.import_bill(("﻿" + WECHAT).encode("utf-8"), "wechat.csv")
    assert (second["added"], second["source"], second["uncategorized"]) == (3, "微信支付", 2)
    rows = {t["merchant"]: t for t in ledger.transactions("2026-11")["transactions"]}
    assert (rows["兰州拉面"]["category"], rows["滴滴出行"]["category"], rows["瑞幸咖啡"]["category"]) == ("餐饮", "交通", "餐饮")
    assert rows["阿强的店"]["amount_cents"] == 123450 and rows["公司"]["direction"] == "income" and rows["瑞幸咖啡"]["note"] == "生椰拿铁"
    assert ledger.summary("2026-11", today=date(2026, 11, 20))["spent"] == 28 + 35.5 + 16 + 300 + 1234.5
    with pytest.raises(ValueError, match="表头"):
        ledger.import_bill("这不是账单".encode())


@pytest.mark.asyncio
async def test_unsorted_payees_are_sorted_by_the_local_model_only(app):
    worker, store = app.state.worker, app.state.store
    client = TestClient(app)
    token = {"Authorization": "Bearer " + app.state.security.local_token}
    sent = client.post("/api/ledger/import", files={"file": ("wechat.csv", WECHAT.encode("utf-8"), "text/csv")}, headers=token).json()
    job = store.get_job(sent["job_id"])
    assert job["kind"] == "ledger" and worker.lane(job) == "local"
    asked = []

    async def complete(messages, **options):
        asked.append(messages[-1]["content"])
        numbers = {line.split(". ")[1].split("（")[0]: line.split(".")[0] for line in asked[0].splitlines()}
        return '好的：' + json.dumps({numbers["老王修车铺"]: "交通", numbers["阿强的店"]: "不存在的类别"}, ensure_ascii=False)
    worker.runtime = SimpleNamespace(complete=complete)
    worker.llm = None  # a cloud client is never touched
    result = await worker.handle(job)
    assert "老王修车铺" in asked[0] and "阿强的店" in asked[0] and result["classified"] == 1
    rows = {t["merchant"]: t["category"] for t in worker.tools.ledger.transactions("2026-11")["transactions"]}
    assert rows["老王修车铺"] == "交通" and rows["阿强的店"] == "其他" and worker.tools.ledger.unsorted() == []
    page = client.get("/api/ledger?period=2026-11", headers=token).json()
    assert len(page["transactions"]) == 3 and page["summary"]["spent"] == 1550.5
    assert client.patch("/api/ledger/transactions/" + page["transactions"][0]["id"], json={"category": "购物"}, headers=token).status_code == 200
    assert client.delete("/api/ledger/transactions/" + page["transactions"][0]["id"], headers=token).status_code == 200
    assert client.get("/api/ledger?period=昨天", headers=token).status_code == 400


@pytest.mark.asyncio
async def test_logging_a_payment_in_conversation_shows_a_card_and_the_month(app):
    model = ScriptedModel([
        Completion("", [call("log_expense", amount=35, category="餐饮", note="午饭")], "tool_calls"),
        Completion("记上了。", [], "stop"),
        Completion("", [call("set_budget", category="餐饮", amount=1000), call("spending_summary", period="this_month")], "tool_calls"),
        Completion("这个月花了 35 元，都在餐饮上。", [], "stop"),
    ])
    _, message = await run(app, model, "午饭 35")
    logged = message["metadata"]["steps"][0]["calls"][0]
    assert logged["card"]["kind"] == "expense" and logged["card"]["value"] == "¥35" and logged["card"]["rows"][1] == {"label": "本月合计", "value": "¥35"}
    assert json.loads(logged["output"])["month_spent"] == 35.0 and "card" not in json.loads(logged["output"])
    assert {"log_expense", "spending_summary", "set_budget", "ledger_transactions"} <= {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert "never add, subtract or estimate" in model.calls[0]["messages"][0]["content"]
    _, message = await run(app, model, "餐饮预算一千，这个月花了多少")
    budget, summary = message["metadata"]["steps"][0]["calls"]
    assert budget["card"]["title"] == "每月餐饮预算" and summary["card"]["kind"] == "ledger" and summary["card"]["value"] == "¥35"


@pytest.mark.asyncio
async def test_a_cloud_model_gets_totals_but_never_single_payments(app):
    worker, store = app.state.worker, app.state.store
    worker.tools.ledger.add(88, "餐饮", "秘密小馆", "和某人吃饭")
    local = ScriptedModel([Completion("", [call("ledger_transactions", period="this_month")], "tool_calls"), Completion("这个月只有一笔，在秘密小馆。", [], "stop")])
    _, message = await run(app, local, "这个月都有哪几笔")
    assert "秘密小馆" in message["metadata"]["steps"][0]["calls"][0]["output"]  # the local model may read the list

    # the same conversation, now on a cloud model
    sent = []

    async def stream(messages, *, model=None, tools=None, **options):
        sent.append({"messages": messages, "tools": tools})
        if len(sent) == 1:
            return Completion("", [call("ledger_transactions", period="this_month"), call("spending_summary", period="this_month")], "tool_calls")
        return Completion("这个月餐饮花了 88 元，明细请在账本里看。", [], "stop")
    worker.agent.llm = SimpleNamespace(chat_model=lambda m=None: "miavo:some-model", is_local=lambda m=None: m in (None, "local"), searches=lambda m=None: False, stream=stream)
    store.execute("UPDATE sessions SET model='miavo:some-model' WHERE id='s1'")
    _, message = await run(app, local, "那总共多少")
    offered = {t["function"]["name"] for t in sent[0]["tools"]}
    assert "spending_summary" in offered and "ledger_transactions" not in offered
    everything = json.dumps([s["messages"] for s in sent], ensure_ascii=False)
    assert "秘密小馆" not in everything.replace("这个月只有一笔，在秘密小馆。", "") and "和某人吃饭" not in everything  # neither replayed nor returned
    assert "Kept on the user's Mac" in everything and "not available to you" in sent[0]["messages"][0]["content"]
    refused, summary = message["metadata"]["steps"][0]["calls"]
    assert refused["status"] == "error" and summary["status"] == "ok" and "秘密小馆" not in summary["output"]


@pytest.mark.asyncio
async def test_saying_it_was_recorded_without_recording_it_is_sent_back_once(app):
    model = ScriptedModel([
        Completion("记下了，午饭 35 元已入账。", [], "stop"),
        Completion("", [call("log_expense", amount=35, category="餐饮", note="午饭")], "tool_calls"),
        Completion("记上了，午饭 35 元。", [], "stop"),
    ])
    _, message = await run(app, model, "午饭 35")
    assert "did not call log_expense" in model.calls[1]["messages"][-1]["content"]
    assert message["content"] == "记上了，午饭 35 元。" and app.state.worker.tools.ledger.summary("this_month")["spent"] == 35.0
