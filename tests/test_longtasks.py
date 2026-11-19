"""Long tasks: writing that exceeds one model call, documents updated across turns, plans in goal mode."""
import json

import pytest

from engine.agent import CUTS, similar, shorten_arguments
from engine.runtime import Completion
from test_agent import ScriptedModel, app, call, chat_job, run  # noqa: F401


def named(ident, tool, **args):
    return {"id": ident, "name": tool, "arguments": json.dumps(args, ensure_ascii=False)}


def episode(number):
    """A page of text that shares almost nothing with the other episodes."""
    line = lambda n: "".join(chr(0x4e00 + (number * 977 + n * 131 + k * 37) % 4000) for k in range(24))
    return f"# 第{number}集\n" + "".join(line(n) + "。\n" for n in range(30))


def workspace(app, name):
    return (app.state.worker.settings.data / "workspace" / name).read_text()


@pytest.mark.asyncio
async def test_a_reply_cut_off_by_the_output_limit_is_continued_in_place(app):
    model = ScriptedModel([Completion("很久以前，", [], "length"), Completion("有一个邮差。", [], "length"), Completion("他送完了最后一封信。", [], "stop")])
    _, message = await run(app, model, "写一个长故事")
    assert message["content"] == "很久以前，有一个邮差。他送完了最后一封信。"
    assert len(message["metadata"]["steps"]) == 1 and not message["metadata"].get("truncated")
    assert "Continue exactly where it stopped" in model.calls[1]["messages"][-1]["content"]
    assert model.calls[1]["messages"][-2] == {"role": "assistant", "content": "很久以前，"}


@pytest.mark.asyncio
async def test_a_tool_call_cut_off_mid_arguments_does_not_run_and_is_redone_in_parts(app):
    model = ScriptedModel([
        Completion("我来写剧本。", [], "length", cut=True),                                             # the server dropped the unfinished call
        Completion("", [{"id": "c0", "name": "save_file", "arguments": '{"name": "a.md", "content": "被截断'}], "length"),  # cut inside the JSON
        Completion("", [named("c1", "save_file", name="a.md", content=episode(1))], "tool_calls"),
        Completion("", [named("c2", "append_file", name="a.md", content=episode(2))], "tool_calls"),
        Completion("写好了，保存在 a.md。", [], "stop"),
    ])
    _, message = await run(app, model, "写两集剧本保存成 a.md")
    text = workspace(app, "a.md")
    assert "# 第1集" in text and "# 第2集" in text and "被截断" not in text
    for cut in (1, 2):
        assert "cut off by the output limit" in model.calls[cut]["messages"][-1]["content"]
    assert message["status"] == "complete" and message["content"].endswith("保存在 a.md。")
    assert [c["name"] for s in message["metadata"]["steps"] for c in s["calls"]] == ["save_file", "append_file"]


@pytest.mark.asyncio
async def test_repeated_cut_offs_end_with_an_answer(app):
    model = ScriptedModel([Completion("", [], "length", cut=True)] * CUTS + [Completion("这份内容太长，我没能保存。", [], "stop")])
    _, message = await run(app, model, "写一本书保存成 book.md")
    assert message["status"] == "complete" and "没能保存" in message["content"]
    assert model.calls[-1]["tools"] is None


@pytest.mark.asyncio
async def test_copying_the_previous_part_into_a_new_file_is_refused(app):
    copy = episode(1).replace("# 第1集", "# 第2集")
    model = ScriptedModel([
        Completion("", [named("c1", "save_file", name="ep1.md", content=episode(1))], "tool_calls"),
        Completion("", [named("c2", "save_file", name="ep2.md", content=copy)], "tool_calls"),
        Completion("", [named("c3", "save_file", name="ep2.md", content=episode(2))], "tool_calls"),
        Completion("两集都写好了。", [], "stop"),
    ])
    _, message = await run(app, model, "写两集，分别保存")
    calls = [c for s in message["metadata"]["steps"] for c in s["calls"]]
    assert [c["status"] for c in calls] == ["ok", "error", "ok"]
    assert "almost identical" in calls[1]["output"] and "ep1.md" in calls[1]["output"]
    assert workspace(app, "ep2.md") == episode(2)


def test_similarity_ignores_short_or_different_text():
    assert similar(episode(1), episode(1).replace("第1集", "第9集", 1))
    assert not similar(episode(1), episode(2)) and not similar("好的", "好的")


@pytest.mark.asyncio
async def test_updating_a_document_in_a_later_turn_changes_it_in_place(app):
    await run(app, ScriptedModel([Completion("", [named("c1", "save_file", name="s.md", content="女主叫林晓。" * 300)], "tool_calls"), Completion("好了。", [], "stop")]), "写剧本存成 s.md")
    model = ScriptedModel([
        Completion("", [named("c2", "edit_file", name="s.md", old_text="林晓", new_text="沈知意", replace_all=True)], "tool_calls"),
        Completion("名字都改好了。", [], "stop"),
    ])
    _, message = await run(app, model, "把女主名字改成沈知意，更新 s.md")
    text = workspace(app, "s.md")
    assert "沈知意" in text and "林晓" not in text
    assert [p.name for p in (app.state.worker.settings.data / "workspace").iterdir() if p.is_file()] == ["s.md"]
    # The earlier turn is replayed with the document shortened: the file holds the full text.
    replayed = next(m for m in model.calls[0]["messages"] if m.get("tool_calls"))
    assert len(replayed["tool_calls"][0]["function"]["arguments"]) < 1000 and "read the file" in replayed["tool_calls"][0]["function"]["arguments"]


def test_shortened_arguments_keep_small_values():
    assert shorten_arguments({"name": "a.md", "content": "x" * 5000})["name"] == "a.md"
    assert shorten_arguments({"query": "短"}) == {"query": "短"}


@pytest.mark.asyncio
async def test_goal_cannot_finish_with_plan_items_left_or_copied_files(app):
    plan = lambda done: named("p" + done, "update_plan", **({"done": done} if done else {"steps": "1. 写 ep1.md\n- 写 ep2.md"}))
    done = named("f", "finish_goal", status="done", summary="两集都写好了。", evidence="ep1.md、ep2.md 已保存")
    model = ScriptedModel([
        Completion("", [plan("")], "tool_calls"),
        Completion("", [named("c1", "save_file", name="ep1.md", content=episode(1))], "tool_calls"),
        Completion("", [done], "tool_calls"),                                           # one item still pending: refused before any review
        Completion("", [named("c2", "save_file", name="ep2.md", content=episode(2))], "tool_calls"),
        Completion("", [plan("1, 2")], "tool_calls"),
        Completion("", [{**done, "id": "f2"}], "tool_calls"),
        Completion('好的。{"achieved": true, "missing": ""', [], "length"),             # verdict cut off: still readable
    ])
    _, message = await run(app, model, "写两集剧本，保存成 ep1.md 和 ep2.md", mode="goal")
    assert message["metadata"]["goal"]["status"] == "done"
    assert message["metadata"]["plan"] == [{"step": "写 ep1.md", "status": "done"}, {"step": "写 ep2.md", "status": "done"}]
    assert "unfinished items: 写 ep1.md; 写 ep2.md" in model.calls[3]["messages"][-1]["content"]
    review = model.calls[-1]["messages"][-1]["content"]
    assert "- ep1.md: " in review and "- ep2.md: " in review and "NEAR-IDENTICAL" not in review
    assert "update_plan" in {t["function"]["name"] for t in model.calls[0]["tools"]}


@pytest.mark.asyncio
async def test_plan_tool_is_not_offered_outside_goal_mode(app):
    model = ScriptedModel([Completion("你好。", [], "stop")])
    await run(app, model, "你好")
    assert not {"update_plan", "finish_goal"} & {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert {"save_file", "append_file", "edit_file"} <= {t["function"]["name"] for t in model.calls[0]["tools"]}


@pytest.mark.asyncio
async def test_an_empty_reply_is_retried_once_before_failing(app):
    model = ScriptedModel([Completion("", [], "stop"), Completion("在的。", [], "stop")])
    _, message = await run(app, model, "在吗")
    assert message["content"] == "在的。" and len(message["metadata"]["steps"]) == 1


@pytest.mark.asyncio
async def test_unfinished_job_status_tells_the_model_not_to_guess(app):
    tools = app.state.worker.tools
    job = app.state.store.create_job("plan", {"project_id": "missing"})
    assert "do not check again" in tools.execute("job_status", {"job_id": job["id"]})["note"]


@pytest.mark.asyncio
async def test_drama_plan_that_fails_validation_is_sent_back_and_repaired(app):
    worker, store = app.state.worker, app.state.store
    shot = {"id": "s1", "title": "开场", "description": "外卖员骑车", "duration": 40}
    short = "```json\n" + json.dumps({"shots": [{**shot, "duration": 12}, {**shot, "id": "s2", "duration": 10}]}) + "\n```"
    replies = [json.dumps({"shots": [shot]}), short, short]  # too long a shot, then far short of 3 minutes twice: the last plan is kept
    seen = []

    async def complete(messages, **options):
        seen.append(list(messages))
        return replies.pop(0)
    worker.llm.complete = complete
    project = worker.tools.execute("create_drama", {"title": "听见", "brief": "外卖员听见心声，3 分钟"})
    result = await worker.handle(store.get_job(project["job_id"]))
    assert result["shots"] == 2 and len(seen) == 3
    assert "shots.0.duration" in seen[1][-1]["content"] and seen[1][-2]["role"] == "assistant"
    assert "只有22秒" in seen[2][-1]["content"] and "180秒" in seen[2][-1]["content"]
    done = store.get_job(project["job_id"])
    store.update_job(done["id"], status="succeeded", result=result)
    assert [s["title"] for s in worker.tools.execute("job_status", {"job_id": done["id"]})["drama"]["shots"]] == ["开场", "开场"]


@pytest.mark.asyncio
async def test_a_piece_far_shorter_than_the_length_asked_for_is_extended_in_place(app):
    model = ScriptedModel([Completion("甲" * 1200, [], "stop"), Completion("乙" * 1500, [], "stop"), Completion("丙" * 1500, [], "stop")])
    _, message = await run(app, model, "直接写一篇 5000 字的小说")
    assert len(model.calls) == 3 and message["content"].count("甲") == 1200 and message["content"].endswith("丙" * 1500)
    assert "about 5000" in model.calls[1]["messages"][-1]["content"] and len(message["metadata"]["steps"]) == 1
    short = ScriptedModel([Completion("好的，每集 1000 字没问题，要什么题材？", [], "stop")])
    await run(app, short, "每集 1000 字，写 3 集")
    assert len(short.calls) == 1


@pytest.mark.asyncio
async def test_rewording_a_search_that_returns_the_same_nothing_is_stopped(app):
    searches = [Completion("", [named(f"r{n}", "recall", query="剧本 " * (n + 1))], "tool_calls") for n in range(3)]
    model = ScriptedModel(searches + [Completion("没有找到那份剧本。", [], "stop")])
    _, message = await run(app, model, "把剧本里的名字改掉")
    assert len(model.calls) == 4 and model.calls[-1]["tools"] is None and "没有找到" in message["content"]


@pytest.mark.asyncio
async def test_a_tool_call_the_server_could_not_read_is_sent_again(app):
    model = ScriptedModel([
        Completion("", [], "stop", bad="invalid character '，' (U+FF0C) (<unknown>, line 1)"),
        Completion("", [named("c1", "save_file", name="a.md", content="第一版")], "tool_calls"),
        Completion("保存好了。", [], "stop"),
    ])
    _, message = await run(app, model, "写一句话保存成 a.md")
    assert workspace(app, "a.md") == "第一版" and message["content"] == "保存好了。"
    assert "could not be read" in model.calls[1]["messages"][-1]["content"] and "U+FF0C" in model.calls[1]["messages"][-1]["content"]
    assert [len(s["calls"]) for s in message["metadata"]["steps"]] == [1, 0]  # the failed attempt leaves no empty step


def test_the_servers_parse_errors_are_recognised_as_unreadable_calls():
    from engine.runtime import _BAD_CALL
    for error in ("invalid character '，' (U+FF0C) (<unknown>, line 1)", "closing parenthesis '}' does not match opening parenthesis '[' (<unknown>, line 1)",
                  "Expecting ',' delimiter: line 1 column 20 (char 19)", "Tool arguments changed after they were streamed."):
        assert _BAD_CALL.search(error), error
    assert not _BAD_CALL.search("model not loaded") and not _BAD_CALL.search("out of memory")


@pytest.mark.asyncio
async def test_a_saved_part_is_shelved_and_a_refused_copy_lists_what_exists(app):
    first, second = "## 第1集：归来\n" + episode(1) * 3, "## 第2集：对峙\n" + episode(2) * 3
    model = ScriptedModel([
        Completion("", [named("c1", "save_file", name="r.md", content=first)], "tool_calls"),
        Completion("", [named("c2", "append_file", name="r.md", content=second)], "tool_calls"),
        Completion("", [named("c3", "append_file", name="r.md", content=second.replace("第2集：对峙", "第3集：反击"))], "tool_calls"),
        Completion("", [], "length", cut=True),
        Completion("写了两集，第三集还没写完。", [], "stop"),
    ])
    _, message = await run(app, model, "写十集剧本保存成 r.md")
    sent = model.calls[2]["messages"]
    shelved = [c["function"]["arguments"] for m in sent for c in m.get("tool_calls") or []]
    assert all(len(a) < 1500 and "saved in the file" in a for a in shelved) and second[-200:] in json.loads(shelved[1])["content"]
    refusal = model.calls[3]["messages"][-1]["content"]
    assert "第1集：归来" in refusal and "第2集：对峙" in refusal and model.calls[3]["temperature"] == .9
    assert "r.md (" in model.calls[4]["messages"][-1]["content"] and "append_file" in model.calls[4]["messages"][-1]["content"]
    assert workspace(app, "r.md").count("第2集：对峙") == 1 and "第3集" not in workspace(app, "r.md") and message["metadata"]["steps"][0]["calls"][0]["arguments"]["content"] == first


@pytest.mark.asyncio
async def test_work_done_is_reported_when_the_model_ends_without_a_word(app):
    edit = Completion("", [named("c1", "save_file", name="n.md", content="一行")], "tool_calls")
    model = ScriptedModel([edit, Completion("", [], "stop"), Completion("", [], "stop"), Completion("", [], "stop")])
    _, message = await run(app, model, "写一行保存成 n.md")
    assert message["status"] == "complete" and message["content"].startswith("已完成：保存文件（已保存为 n.md")


@pytest.mark.asyncio
async def test_a_slip_in_a_tool_name_still_reaches_the_tool(app):
    model = ScriptedModel([Completion("", [named("c1", "_list_files")], "tool_calls"), Completion("工作区是空的。", [], "stop")])
    _, message = await run(app, model, "工作区有什么")
    assert message["metadata"]["steps"][0]["calls"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_continuing_stops_when_the_model_goes_round_in_circles(app):
    loop = "".join(chr(0x4e00 + (n * 37) % 3000) for n in range(600))
    model = ScriptedModel([Completion(loop, [], "length"), Completion(loop, [], "length"), Completion("不会走到这里", [], "stop")])
    _, message = await run(app, model, "写一个长故事")
    assert message["content"] == loop and len(model.calls) == 2


@pytest.mark.asyncio
async def test_write_document_writes_each_section_in_its_own_context_and_never_keeps_a_copy(app):
    write = named("w", "write_document", brief="古装宫斗，女主沈昭宁。", outline="1. 第1集：入宫\n2. 第2集：暗流\n第3集：反击", length=700,
                  files="ep1.md\nep2.md\nep3.md")
    model = ScriptedModel([
        Completion("", [write], "tool_calls"),
        Completion(episode(1), [], "stop"),
        Completion(episode(1), [], "stop"),   # section 2 comes out as a copy of section 1: asked again
        Completion(episode(2), [], "stop"),
        Completion(episode(3)[:400], [], "length"), Completion(episode(3)[400:], [], "stop"),  # cut off mid-section: continued
        Completion("三集都写好了。", [], "stop"),
    ])
    _, message = await run(app, model, "写三集宫斗剧本，每集一个文件")
    assert [workspace(app, f"ep{n}.md").strip() for n in (1, 2, 3)] == [episode(n).strip() for n in (1, 2, 3)]
    entry = message["metadata"]["steps"][0]["calls"][0]
    assert entry["status"] == "ok" and entry["summary"].startswith("3 部分") and [s["file"] for s in entry["sections"]] == ["ep1.md", "ep2.md", "ep3.md"]
    second = model.calls[2]["messages"]
    assert len(second) == 2 and second[0]["role"] == "system" and model.calls[2]["tools"] is None
    assert "section 2 of 3: 第2集：暗流" in second[1]["content"] and "沈昭宁" in second[1]["content"] and episode(1)[-200:] in second[1]["content"]
    assert episode(1)[:200] not in second[1]["content"]  # only how the last section ended, never the whole of it
    assert "repeated an earlier section" in model.calls[3]["messages"][1]["content"]
    assert json.loads(model.calls[-1]["messages"][-1]["content"])["sections_written"] == 3


@pytest.mark.asyncio
async def test_write_document_into_one_file_reports_honestly_when_a_section_keeps_repeating(app):
    write = named("w", "write_document", name="r.md", brief="复仇剧", outline="第1集：归来\n第2集：对峙\n第3集：反击")
    model = ScriptedModel([Completion("", [write], "tool_calls"), Completion(episode(1), [], "stop"), Completion(episode(2), [], "stop")]
                          + [Completion(episode(2), [], "stop")] * 3 + [Completion("只写完了前两集。", [], "stop")])
    _, message = await run(app, model, "写三集保存成 r.md")
    text = workspace(app, "r.md")
    assert episode(1).strip() in text and text.count(episode(2).strip()) == 1
    entry = message["metadata"]["steps"][0]["calls"][0]
    assert entry["status"] == "error" and entry["summary"] == "只写完 2/3 部分"
    assert "Stopped at section 3 of 3" in model.calls[-1]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_an_edit_whose_old_text_is_close_but_not_exact_still_lands_on_the_passage_meant(app):
    tools = app.state.worker.tools
    tools.save_file("s.md", "第 2 集\n\n林知夏：（转身，声音平稳）只是路过，被铃声吸引。\n\n周叙：（微笑）欢迎下次再来。\n\n第 3 集\n\n老陈：你姐姐那晚给我打过电话。\n")
    result = tools.edit_file("s.md", "（林知夏转身，声音平稳）只是路过，被铃声吸引。\n\n周叙：（微笑）欢迎下次再来。", "周叙：（轻声）录音笔里，只有您自己的声音。")
    text = workspace(app, "s.md")
    assert "只有您自己的声音" in text and "欢迎下次再来" not in text and "老陈：你姐姐" in text
    assert result["replaced"].startswith("林知夏：（转身")
    tools.edit_file("s.md", "第 3 集\\n\\n老陈：你姐姐那晚给我打过电话。", "第 3 集\\n\\n老陈：别听第七声。")   # newlines sent as backslash-n
    assert "第 3 集\n\n老陈：别听第七声。" in workspace(app, "s.md")
    with pytest.raises(ValueError, match="append_file"):
        tools.edit_file("s.md", "老陈：别听第七声。", "老陈：别听第七声。")
    with pytest.raises(ValueError, match="找不到"):
        tools.edit_file("s.md", "这段话和文件里的任何内容都没有关系，完全是另外一回事。", "x")


@pytest.mark.asyncio
async def test_a_turn_that_ends_after_a_failed_write_is_pushed_to_make_the_change(app):
    app.state.worker.tools.save_file("plan.md", "# 店庆方案\n\n预算 3000 元。\n")
    model = ScriptedModel([
        Completion("", [named("c1", "edit_file", name="plan.md", old_text="完全不存在的一段话，和文件毫无关系的内容", new_text="x")], "tool_calls"),
        Completion("好，在文档末尾追加一节「风险预案」。", [], "stop"),
        Completion("", [named("c2", "append_file", name="plan.md", content="\n## 风险预案\n\n下雨改到室内。\n")], "tool_calls"),
        Completion("已加上「风险预案」一节。", [], "stop"),
    ])
    _, message = await run(app, model, "在 plan.md 里加一节风险预案")
    assert "## 风险预案" in workspace(app, "plan.md")
    assert "The file was not changed" in model.calls[2]["messages"][-1]["content"]
    assert message["content"].endswith("已加上「风险预案」一节。")


@pytest.mark.asyncio
async def test_write_document_refuses_one_file_and_many_files_at_once(app):
    model = ScriptedModel([
        Completion("", [named("c1", "write_document", name="revenge.md", brief="复仇短剧", outline="第 1 集：归来\n第 2 集：旧宅", files="ep1.md\nep2.md")], "tool_calls"),
        Completion("参数有冲突，我没有写。", [], "stop"),
    ])
    _, message = await run(app, model, "写两集保存成 revenge.md")
    call = message["metadata"]["steps"][0]["calls"][0]
    assert call["status"] == "error" and "name only" in call["output"]
    assert len(model.calls) == 2  # no section was written


@pytest.mark.asyncio
async def test_write_document_keeps_to_the_one_file_the_user_named(app):
    split = named("c1", "write_document", brief="复仇短剧", outline="第 1 集：归来\n第 2 集：旧宅", files="ep1.md\nep2.md")
    whole = named("c2", "write_document", name="revenge.md", brief="复仇短剧", outline="第 1 集：归来\n第 2 集：旧宅")
    model = ScriptedModel([Completion("", [split], "tool_calls"), Completion("", [whole], "tool_calls"),
                           Completion(episode(1), [], "stop"), Completion(episode(2), [], "stop"), Completion("写好了。", [], "stop")])
    _, message = await run(app, model, "写两集剧本，保存成一个文件 revenge.md")
    first, second = message["metadata"]["steps"][0]["calls"][0], message["metadata"]["steps"][1]["calls"][0]
    assert first["status"] == "error" and "revenge.md" in first["output"] and second["status"] == "ok"
    assert episode(2).strip() in workspace(app, "revenge.md")
    assert not (app.state.worker.settings.data / "workspace" / "ep1.md").exists()


@pytest.mark.asyncio
async def test_a_value_whose_parameter_name_was_dropped_still_reaches_the_tool(app):
    model = ScriptedModel([Completion("", [{"id": "c1", "name": "read_file", "arguments": json.dumps({"": "a.md"})}], "tool_calls"), Completion("读到了。", [], "stop")])
    app.state.worker.tools.save_file("a.md", "内容")
    _, message = await run(app, model, "读一下 a.md")
    assert message["metadata"]["steps"][0]["calls"][0]["status"] == "ok"
