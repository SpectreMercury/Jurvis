"""Short-drama production order: character sheets lock identity, keyframes inherit it, video animates keyframes."""
import zlib

FRAME_SIZE=(704,1216)  # Twice the 352x608 video canvas, so keyframes reach the video model uncropped.
MAX_CAST=4
SHEET="Character design reference. One single character, full body from head to toe, front view, standing straight, neutral expression, plain pure white background, no scenery, no text."
KEEP="Keep every character exactly as in the reference pictures: same face, body proportions, hairstyle, costume and colours. Only pose, expression, camera and background change."


def look(char):
    return (char.get("appearance") or char.get("image_prompt") or char["description"]).strip()


def seed(*parts):
    return zlib.crc32(":".join(parts).encode())


def find(items,ident):
    return next((x for x in items if x["id"]==ident),None)


def cast(doc,shot):
    return [c for c in (find(doc.get("characters",[]),i) for i in shot.get("character_ids",[])) if c]


def references(doc,shot):
    return [c["reference_asset"] for c in cast(doc,shot) if c.get("reference_asset")]


def require_cast(doc,shot):
    people=cast(doc,shot)
    if len(people)>MAX_CAST:
        raise ValueError(f"「{shot['title']}」出场角色超过 {MAX_CAST} 个，请拆分镜头")
    missing=[c["name"] for c in people if not c.get("reference_asset")]
    if missing:
        raise ValueError("请先生成角色定妆图："+"、".join(missing))
    return people


def character_request(doc,char):
    width,height=FRAME_SIZE
    prompt=f"{doc.get('style','')}. {SHEET} {char.get('image_prompt') or look(char)}"
    return {"prompt":prompt,"width":width,"height":height,"steps":25,"seed":seed(char["id"],char["name"]),"reference_assets":[],"character_id":char["id"]}


def frame_request(doc,shot):
    people=require_cast(doc,shot)
    width,height=FRAME_SIZE
    names=" ".join(f"Picture {n} is {c['name']}: {look(c)}." for n,c in enumerate(people,1))
    prompt=" ".join(x for x in (f"{doc.get('style','')}.",names,shot.get("image_prompt") or shot["description"],KEEP if people else "") if x)
    return {"prompt":prompt,"width":width,"height":height,"steps":25,"seed":seed(shot["id"]),"reference_assets":references(doc,shot),"shot_id":shot["id"]}


def frame_stale(doc,shot):
    return not shot.get("first_frame") or shot.get("frame_references",[])!=references(doc,shot)


def locked_prompt(doc,shot,prompt):
    people=cast(doc,shot)
    if not people:
        return prompt
    return "Characters, unchanged from the first frame: "+" ".join(f"{c['name']}: {look(c)}." for c in people)+"\n"+prompt


def video_request(doc,shot,settings):
    prompt=f"{doc.get('style','')}. {shot.get('video_prompt') or shot['description']}\nExact dialogue: {shot.get('dialogue','')}"
    return {**settings,"prompt":prompt,"duration":shot["duration"],"first_frame":shot.get("first_frame"),"last_frame":shot.get("last_frame"),"seed":seed(shot["id"]),"shot_id":shot["id"]}
