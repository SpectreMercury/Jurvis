"""The character that stands for the assistant in the app: drawn once from the user's description, then kept.

The user describes it (a person, a cat, a dog, anything) or gives a photo; the image model draws a few candidates
on a flat key colour, the background is cut away, and from the one the user picks a small set of faces is drawn
with the pick as the reference image, so they are all the same character. What it says to itself (engine/journal.py)
appears beside it, with the face that fits.
"""
import json
import os
import secrets
import time

import numpy as np
from PIL import Image, ImageFilter

from .store import new_id

SIZE = 768
CANDIDATES = 3
DEFAULT_NAME = "小维"
# The moods a journal line can carry (journal.MOODS) and how each face is asked for. "calm" is the picked drawing itself.
FACES = {
    "thinking": "thoughtful, eyes looking up to one side, one hand or paw raised to the chin",
    "troubled": "worried and troubled, eyebrows knitted, a small frown, one sweat drop on the forehead",
    "happy": "very happy, eyes closed in joyful arcs, a big open smile",
    "guilty": "guilty and sheepish, eyes looking away to the side, an awkward wavy smile",
    "sleepy": "sleepy and idle, eyes half closed, a small yawn",
}
KEYS = {"green": ("flat solid bright green background (#00FF00)", (0, 255, 0)), "magenta": ("flat solid bright magenta background (#FF00FF)", (255, 0, 255))}
GREENISH = ("green", "绿", "青蛙", "frog", "恐龙", "dinosaur", "鳄鱼", "crocodile", "仙人掌", "cactus", "乌龟", "turtle", "草", "leaf", "叶", "植物", "plant", "外星", "alien", "蜥蜴", "lizard")
STYLE = ("Sticker illustration of a single cute character, whole body from head to feet fully visible, centered, facing the viewer, soft flat colors, "
         "thick clean dark outline, simple shapes, {background} filling the whole image, no shadow, no ground, no text, no other objects. ")
SAME = ("Keep the exact same character, same art style, same colors, same clothes and accessories, same pose, same framing, same {background}. "
        "Change only the facial expression: ")
NEAR, FAR = 70, 150     # colour distance from the key: transparent below NEAR, solid above FAR
KEPT = (.04, .7)        # share of the picture a cut-out character may fill; outside this the key failed


def cut(image, box=None):
    """(cut-out RGBA, share of the picture kept, the character's box). The background is whatever colour the border has."""
    rgb = np.asarray(image.convert("RGB")).astype(np.float32)
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    key = np.median(border, axis=0)
    distance = np.sqrt(((rgb - key) ** 2).sum(-1))
    alpha = np.clip((distance - NEAR) / (FAR - NEAR), 0, 1)
    kept = float((alpha > .5).mean())
    # A pixel on the edge is part character, part background: take the background back out of its colour, so no fringe is left.
    soft = (alpha > 0) & (alpha < 1)
    rgb[soft] = np.clip((rgb[soft] - (1 - alpha[soft, None]) * key) / np.maximum(alpha[soft, None], .15), 0, 255)
    # Solid pixels right at the edge still carry a tint of the key: pull the key's dominant channels down to the others there.
    mask = Image.fromarray((alpha * 255).astype(np.uint8))
    rim = (np.asarray(mask.filter(ImageFilter.MinFilter(5))) < 255) & (alpha > 0)
    strong = key > 128
    if strong.any() and not strong.all():
        ceiling = rgb[..., ~strong].max(-1)
        for channel in np.flatnonzero(strong):
            plane = rgb[..., channel]
            plane[rim] = np.minimum(plane[rim], np.maximum(ceiling[rim], 0) + 12)
    out = Image.fromarray(np.dstack([rgb, alpha * 255]).astype(np.uint8), "RGBA")
    if box is None:
        found = Image.fromarray(((alpha > .5) * 255).astype(np.uint8)).getbbox() or (0, 0, *image.size)
        side = max(found[2] - found[0], found[3] - found[1]) * 1.1
        cx, cy = (found[0] + found[2]) / 2, (found[1] + found[3]) / 2
        box = [round(cx - side / 2), round(cy - side / 2), round(cx + side / 2), round(cy + side / 2)]
    return out.crop(tuple(box)).resize((512, 512), Image.Resampling.LANCZOS), kept, list(box)


class Avatar:
    def __init__(self, settings, store, media=None):
        self.settings, self.store, self.media = settings, store, media
        self.path = settings.data / "avatar.json"

    def state(self):
        try:
            value = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            value = {}
        return {"name": DEFAULT_NAME, "description": "", "status": "none", "candidates": [], "base": None, "faces": {}, "job_id": None, "error": None,
                **(value if isinstance(value, dict) else {})}

    def save(self, **changes):
        value = {**self.state(), **changes}
        temp = self.path.with_suffix(".tmp")
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
            json.dump(value, file, ensure_ascii=False, indent=1)
        os.replace(temp, self.path)
        self.store.event("avatar", "state")
        return value

    def busy(self):
        state = self.state()
        job = self.store.get_job(state["job_id"]) if state.get("job_id") else None
        return bool(job and job["status"] in ("queued", "running"))

    def view(self):
        """The state for the apps. A drawing that stopped (the engine restarted, the job failed) is reported as that, not as still going."""
        state = self.state()
        job = self.store.get_job(state["job_id"]) if state.get("job_id") else None
        if state["status"] in ("drafting", "making") and not (job and job["status"] in ("queued", "running")):
            error = (job or {}).get("error") or "画到一半停下了"
            state = self.save(status="choosing" if state["status"] == "drafting" and state["candidates"] else "ready" if state["base"] else "failed",
                              error=None if state["status"] == "making" and len(state["faces"]) > 1 else error)
        state["progress"] = {"phase": job["phase"], "value": job["progress"]} if job and job["status"] in ("queued", "running") else None
        state["moods"] = ["calm", *FACES]
        return state

    # ---- what the user asks for ----
    def draft(self, description, photo=None):
        description = " ".join(str(description or "").split())[:300]
        if not description and not photo:
            raise ValueError("先说说它长什么样，或者给一张照片")
        if self.busy():
            raise ValueError("上一组还在画，等它画完再来")
        if photo:
            self.media.asset(photo)
        job = self.store.create_job("avatar", {"stage": "draft", "description": description, "photo": photo})
        self.forget(self.state()["candidates"], keep=self.kept())
        self.save(description=description, status="drafting", candidates=[], job_id=job["id"], error=None, photo=photo)
        return self.view()

    def choose(self, asset_id):
        state = self.state()
        if asset_id not in state["candidates"]:
            raise ValueError("请从刚画好的几张里选")
        if self.busy():
            raise ValueError("还在画，等它画完再选")
        old = [state["base"], *state["faces"].values()]
        job = self.store.create_job("avatar", {"stage": "faces", "base": asset_id, "description": state["description"]})
        self.save(base=asset_id, faces={"calm": asset_id}, status="making", job_id=job["id"], error=None)
        self.forget([a for a in old + state["candidates"] if a and a != asset_id])
        self.save(candidates=[])
        return self.view()

    def reset(self):
        if self.busy():
            raise ValueError("还在画，先等它画完")
        state = self.state()
        self.forget([*state["candidates"], state["base"], *state["faces"].values()])
        return self.save(description="", status="none", candidates=[], base=None, faces={}, job_id=None, error=None) and self.view()

    def kept(self):
        state = self.state()
        return {a for a in [state["base"], *state["faces"].values()] if a}

    def forget(self, assets, keep=()):
        """Drawings that were not picked are removed, with the uncut pictures they were made from."""
        for ident in {a for a in assets if a and a not in keep}:
            row = self.store.one("SELECT * FROM assets WHERE id=? AND kind='avatar'", (ident,))
            for asset in filter(None, [row, row and self.store.one("SELECT * FROM assets WHERE id=? AND kind='avatar-raw'", (row["metadata"].get("raw"),))]):
                (self.settings.data / asset["path"]).unlink(missing_ok=True)
                self.store.execute("DELETE FROM assets WHERE id=?", (asset["id"],))

    # ---- the drawing itself (a job in the local lane) ----
    @staticmethod
    def key(description):
        return "magenta" if any(word in description.lower() for word in GREENISH) else "green"

    async def draw(self, prompt, ident, phase, references, box=None):
        """One picture, cut out and kept as an asset. Returns (asset id, box) or None when the background could not be told from the character."""
        for attempt in range(2):
            request = {"prompt": prompt, "width": SIZE, "height": SIZE, "steps": 20, "seed": secrets.randbelow(2**32), "reference_assets": references}
            result = await self.media.run("image", request, ident, phase=phase)
            raw = result["asset_ids"][0]
            self.store.execute("UPDATE assets SET kind='avatar-raw' WHERE id=?", (raw,))
            _, path = self.media.asset(raw)
            with Image.open(path) as image:
                cutout, kept, found = cut(image, box)
            if KEPT[0] <= kept <= KEPT[1]:
                target = self.settings.data / "assets" / (new_id() + ".png")
                cutout.save(target, format="PNG")
                return self.media.record(target, "形象", "image/png", "avatar", ident, {"raw": raw, "width": 512, "height": 512, "box": found}), found
            path.unlink(missing_ok=True)
            self.store.execute("DELETE FROM assets WHERE id=?", (raw,))
        return None

    async def run(self, p, ident):
        description = p.get("description") or ""
        background = KEYS[self.key(description)][0]
        if p["stage"] == "draft":
            subject = ("Redraw the main subject of the reference photo as this character, keeping its distinctive colors, markings and features. " if p.get("photo") else "") \
                      + (f"The character: {description}. Calm neutral expression, mouth closed." if description else "Calm neutral expression, mouth closed.")
            made = []
            for n in range(CANDIDATES):
                self.store.update_job(ident, progress=n / CANDIDATES)
                drawn = await self.draw(STYLE.format(background=background) + subject, ident, f"正在画第 {n + 1} 张，共 {CANDIDATES} 张", [p["photo"]] if p.get("photo") else [])
                if drawn:
                    made.append(drawn[0])
                    self.save(candidates=made)
            if not made:
                self.save(status="failed", error="几张都没能把形象从背景里分出来，换个说法再试一次")
                raise RuntimeError("形象没有画成")
            self.save(status="choosing")
            return {"candidates": made}
        base = self.store.one("SELECT * FROM assets WHERE id=? AND kind='avatar'", (p["base"],))
        if not base:
            raise ValueError("选中的形象不见了")
        faces, started = {"calm": p["base"]}, time.monotonic()
        for n, (mood, look) in enumerate(FACES.items()):
            if self.state()["base"] != p["base"]:
                break  # the user picked another character meanwhile
            self.store.update_job(ident, progress=n / len(FACES))
            try:
                drawn = await self.draw(SAME.format(background=background) + look + ".", ident, f"正在画表情 {n + 1}/{len(FACES)}", [base["metadata"]["raw"]], base["metadata"].get("box"))
            except RuntimeError:
                drawn = None  # one face that did not come out is not worth losing the others: the calm face stands in
            if drawn:
                faces[mood] = drawn[0]
                self.save(faces=faces)
        self.save(status="ready")
        return {"faces": faces, "seconds": round(time.monotonic() - started)}
