from __future__ import annotations

import asyncio
import math
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.clients.ffmpeg import create_silence, run
from app.clients.storage import AssetStore
from app.config import Settings
from app.core.story import fallback_story_plan
from app.providers.base import AssetResult, InspectionResult, PlanResult, StudioProvider
from app.schemas import Character, ProjectBrief, QualityDimension, QualityReport, ShotContract

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


class MockStudioProvider(StudioProvider):
    """Zero-key local studio that executes the same graph, QC, repair, and edit stages."""

    def __init__(self, settings: Settings, store: AssetStore):
        self.settings = settings
        self.store = store

    async def plan_story(self, brief: ProjectBrief) -> PlanResult:
        await asyncio.sleep(0.04)
        return PlanResult(
            plan=fallback_story_plan(brief),
            model="directorgraph-deterministic-story-compiler",
            input_tokens=1850,
            output_tokens=3120,
        )

    def _draw_character_reference(self, path: Path, character: Character) -> None:
        width = height = 1024
        image = Image.new("RGB", (width, height), (8, 12, 24))
        draw = ImageDraw.Draw(image)
        for y in range(height):
            t = y / max(height - 1, 1)
            draw.line([(0, y), (width, y)], fill=(int(8 + 22*t), int(13 + 17*t), int(28 + 34*t)))
        margin = 70
        draw.rounded_rectangle([margin, margin, width-margin, height-margin], radius=38, outline=(76, 94, 132), width=5)
        title_font = self._font(FONT_BOLD, 58)
        body_font = self._font(FONT_REGULAR, 30)
        small_font = self._font(FONT_REGULAR, 24)
        draw.text((margin+35, margin+35), character.name.upper(), font=title_font, fill=(243, 174, 77))
        draw.text((margin+38, margin+112), character.role, font=small_font, fill=(164, 177, 207))
        cx, cy = width//2, height//2 - 20
        if "robot" in (character.appearance + character.role).lower():
            r = 145
            draw.rounded_rectangle([cx-r, cy-r, cx+r, cy+r], radius=50, fill=(196, 188, 164), outline=(73, 99, 135), width=8)
            draw.ellipse([cx-30, cy-78, cx+30, cy-18], fill=(242, 167, 55))
            draw.line([(cx-r//2, cy+r), (cx-r, cy+r+110)], fill=(133, 145, 157), width=14)
            draw.line([(cx+r//2, cy+r), (cx+r, cy+r+110)], fill=(133, 145, 157), width=14)
        else:
            head = 92
            draw.ellipse([cx-head, cy-head*2, cx+head, cy], fill=(191, 151, 126), outline=(93, 66, 59), width=5)
            draw.polygon([(cx-170, cy), (cx+170, cy), (cx+235, cy+320), (cx-235, cy+320)], fill=(33, 78, 64))
        y = 760
        for line in self._wrap(draw, character.appearance, body_font, width-margin*2-60):
            draw.text((margin+35, y), line, font=body_font, fill=(238, 241, 248))
            y += 38
        image.save(path, quality=94)

    async def generate_character_reference(
        self, project_id: str, character: Character, seed: int
    ) -> AssetResult:
        key = f"projects/{project_id}/characters/{character.id}.png"
        path = self.store.path_for_key(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._draw_character_reference, path, character)
        stored = self.store.put_file(path, key)
        return AssetResult(
            stored.public_url,
            stored.local_path,
            "mock",
            "directorgraph-character-lock",
            usage={"image_count": 1, "seed": seed},
            object_key=stored.key,
        )

    @staticmethod
    def _size(ratio: str) -> tuple[int, int]:
        return {"9:16": (720, 1280), "16:9": (1280, 720), "1:1": (1024, 1024)}[ratio]

    @staticmethod
    def _font(path: str, size: int):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            return ImageFont.load_default()

    @staticmethod
    def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
        words, lines, current = text.split(), [], ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        return lines[:5]

    def _draw_frame(
        self,
        path: Path,
        contract: ShotContract,
        *,
        prop_visible: bool = True,
        repaired: bool = False,
    ) -> None:
        width, height = self._size(contract.aspect_ratio)
        image = Image.new("RGB", (width, height), (7, 11, 22))
        draw = ImageDraw.Draw(image)
        for y in range(height):
            t = y / max(height - 1, 1)
            draw.line([(0, y), (width, y)], fill=(int(8 + 18*t), int(13 + 15*t), int(27 + 30*t)))
        margin = int(min(width, height) * 0.055)
        draw.rounded_rectangle([margin, margin, width-margin, height-margin], radius=24, outline=(78, 91, 121), width=3)
        draw.rectangle([margin, margin, width-margin, margin+int(height*0.12)], fill=(16, 23, 43))
        draw.text((margin*1.35, margin*1.25), contract.id, font=self._font(FONT_BOLD, max(28, width//16)), fill=(241, 169, 72))
        draw.text((margin*1.35, margin*2.6), contract.title.upper(), font=self._font(FONT_BOLD, max(16, width//31)), fill=(241, 244, 250))

        top, bottom = int(height*0.25), int(height*0.68)
        left, right = int(width*0.14), int(width*0.86)
        draw.rectangle([left, top, right, bottom], fill=(15, 24, 40), outline=(50, 63, 85), width=2)
        door_x, door_w = int(width*0.60), int(width*0.22)
        draw.rectangle([door_x, int(height*0.32), door_x+door_w, bottom], fill=(39, 34, 43), outline=(125, 103, 86), width=3)
        if contract.sequence >= 5:
            draw.polygon([(door_x, int(height*0.32)), (door_x+door_w, int(height*0.32)), (door_x+door_w+int(width*.07), bottom), (door_x-int(width*.05), bottom)], fill=(89, 58, 33))

        robot_x, robot_y = int(width*0.35), int(height*0.55)
        r = max(22, width//18)
        draw.rounded_rectangle([robot_x-r, robot_y-r, robot_x+r, robot_y+r], radius=r//3, fill=(196, 188, 164), outline=(71, 91, 116), width=3)
        draw.ellipse([robot_x-r//5, robot_y-r//2, robot_x+r//5, robot_y-r//10], fill=(241, 165, 55))
        draw.line([(robot_x-r//2, robot_y+r), (robot_x-r, robot_y+r*2)], fill=(130, 140, 150), width=5)
        draw.line([(robot_x+r//2, robot_y+r), (robot_x+r, robot_y+r*2)], fill=(130, 140, 150), width=5)
        if "C01" in contract.characters:
            px, py, h = int(width*.70), int(height*.49), max(18, width//25)
            draw.ellipse([px-h, py-h*2, px+h, py], fill=(192, 153, 128))
            draw.polygon([(px-h*2, py), (px+h*2, py), (px+h*3, bottom), (px-h*3, bottom)], fill=(31, 76, 61))
        if contract.continuity.required_props and prop_visible:
            x, y, s = int(width*.50), int(height*.67), max(22, width//24)
            draw.polygon([(x, y-s), (x+s, y), (x, y+s//3), (x-s, y)], fill=(208, 52, 59), outline=(255, 137, 115))
            draw.line([(x, y), (x+s*2, y-s)], fill=(239, 92, 81), width=3)
        if repaired:
            draw.rounded_rectangle([margin*1.25, int(height*.18), margin*5.2, int(height*.225)], radius=10, fill=(35, 120, 82))
            draw.text((margin*1.5, int(height*.185)), "SELF-REPAIRED", font=self._font(FONT_BOLD, max(12, width//44)), fill=(246, 255, 248))

        y = int(height*.75)
        body = self._font(FONT_REGULAR, max(17, width//32))
        for line in self._wrap(draw, contract.narrative_objective, body, width-margin*3):
            draw.text((margin * 1.4, y), line, font=body, fill=(235, 239, 247))
            y += getattr(body, "size", 22) + 6
        y += 10
        small = self._font(FONT_REGULAR, max(14, width//42))
        for line in self._wrap(draw, contract.action, small, width-margin*3):
            draw.text((margin * 1.4, y), line, font=small, fill=(155, 168, 194))
            y += getattr(small, "size", 18) + 5
        image.save(path, quality=93)

    async def generate_storyboard(self, project_id: str, contract: ShotContract, seed: int) -> AssetResult:
        key = f"projects/{project_id}/shots/{contract.id}/storyboard.png"
        path = self.store.path_for_key(key)
        await asyncio.to_thread(self._draw_frame, path, contract)
        stored = self.store.put_file(path, key)
        return AssetResult(
            stored.public_url,
            stored.local_path,
            "mock",
            "directorgraph-storyboard-renderer",
            usage={"image_count": 1, "seed": seed},
            object_key=stored.key,
        )

    async def synthesize_voice(self, project_id: str, contract: ShotContract, language: str) -> AssetResult | None:
        text = (contract.dialogue or contract.narration or "").replace("\n", " ").strip()
        if not text:
            return None
        key = f"projects/{project_id}/shots/{contract.id}/dialogue.wav"
        path = self.store.path_for_key(key)
        def speak() -> None:
            try:
                process = subprocess.run(
                    ["espeak", "-s", "142", "-p", "42", "-w", str(path), text],
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError:
                create_silence(path, contract.duration_seconds)
                return
            if process.returncode != 0:
                create_silence(path, contract.duration_seconds)
        await asyncio.to_thread(speak)
        stored = self.store.put_file(path, key)
        return AssetResult(
            stored.public_url,
            stored.local_path,
            "mock",
            "espeak-local-preview",
            usage={"characters": len(text)},
            object_key=stored.key,
        )

    async def _render(self, project_id: str, contract: ShotContract, frame: Path, attempt: int, repaired: bool) -> AssetResult:
        label = f"repair-{attempt}" if repaired else f"attempt-{attempt}"
        key = f"projects/{project_id}/shots/{contract.id}/{label}.mp4"
        output = self.store.path_for_key(key)
        width, height = self._size(contract.aspect_ratio)
        render_w, render_h = ((450, 800) if height > width else (800, 450)) if width != height else (640, 640)
        frames = math.ceil(contract.duration_seconds * 24)
        vf = f"scale={render_w*2}:{render_h*2},zoompan=z='min(zoom+0.0007,1.06)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={render_w}x{render_h}:fps=24,format=yuv420p"
        await asyncio.to_thread(run, [
            "ffmpeg", "-y", "-loop", "1", "-i", str(frame),
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-vf", vf, "-t", str(contract.duration_seconds),
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "27",
            "-c:a", "aac", "-b:a", "64k", "-shortest", "-movflags", "+faststart", str(output),
        ])
        stored = self.store.put_file(output, key)
        return AssetResult(
            stored.public_url,
            stored.local_path,
            "mock",
            "ffmpeg-kinetic-preview",
            usage={"duration": contract.duration_seconds},
            object_key=stored.key,
        )

    async def generate_video(
        self,
        project_id: str,
        contract: ShotContract,
        storyboard: AssetResult,
        audio: AssetResult | None,
        references: list[AssetResult],
        attempt: int,
        repair_instruction: str | None = None,
    ) -> AssetResult:
        frame = storyboard.local_path
        if contract.id == "S05" and attempt == 1:
            frame = self.store.path_for_key(f"projects/{project_id}/shots/{contract.id}/defective-frame.png")
            await asyncio.to_thread(self._draw_frame, frame, contract, prop_visible=False)
        return await self._render(project_id, contract, frame, attempt, repaired=False)

    async def inspect_video(self, contract: ShotContract, video: AssetResult, attempt: int) -> InspectionResult:
        await asyncio.sleep(0.04)
        defect = contract.id == "S05" and attempt == 1 and "repair" not in video.local_path.name
        if defect:
            report = QualityReport(
                passed=False,
                overall_score=0.72,
                dimensions=[
                    QualityDimension(name="narrative", score=.91, evidence="The door opens on cue."),
                    QualityDimension(name="identity", score=.90, evidence="Both characters remain legible."),
                    QualityDimension(name="continuity", score=.38, evidence="The red paper crane is absent."),
                    QualityDimension(name="camera", score=.86, evidence="Reveal framing follows the contract."),
                    QualityDimension(name="motion", score=.83, evidence="Motion is stable."),
                    QualityDimension(name="dialogue", score=1, evidence="No line is required here."),
                    QualityDimension(name="safety", score=1, evidence="No safety violation."),
                ],
                violations=["Required prop 'red paper crane' is missing between the characters."],
                repair_strategy="local_edit",
                repair_instruction="Restore the red paper crane on the parcel. Preserve identity, framing, lighting, and timing.",
                evaluator_model="directorgraph-mock-continuity-supervisor",
                attempt=attempt,
            )
        else:
            report = QualityReport(
                passed=True,
                overall_score=.93,
                dimensions=[
                    QualityDimension(name="narrative", score=.94, evidence="Beat objective is visible."),
                    QualityDimension(name="identity", score=.92, evidence="Character design is stable."),
                    QualityDimension(name="continuity", score=.95, evidence="Required props and state persist."),
                    QualityDimension(name="camera", score=.91, evidence="Framing follows the plan."),
                    QualityDimension(name="motion", score=.89, evidence="Motion is coherent."),
                    QualityDimension(name="dialogue", score=.94, evidence="Audio fits its shot."),
                    QualityDimension(name="safety", score=1, evidence="No safety violation."),
                ],
                evaluator_model="directorgraph-mock-continuity-supervisor",
                attempt=attempt,
            )
        return InspectionResult(report=report, model=report.evaluator_model, input_tokens=540)

    async def repair_video(
        self,
        project_id: str,
        contract: ShotContract,
        video: AssetResult,
        storyboard: AssetResult,
        references: list[AssetResult],
        report: QualityReport,
        attempt: int,
    ) -> AssetResult:
        frame = self.store.path_for_key(f"projects/{project_id}/shots/{contract.id}/repaired-frame.png")
        await asyncio.to_thread(self._draw_frame, frame, contract, prop_visible=True, repaired=True)
        return await self._render(project_id, contract, frame, attempt, repaired=True)
