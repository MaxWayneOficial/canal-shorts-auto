
import os
import json
import random
import time
import re
import asyncio
import subprocess
import textwrap
from pathlib import Path
from html import unescape

import requests
from PIL import Image, ImageDraw, ImageFont
import edge_tts

OUT = Path("output")
OUT.mkdir(exist_ok=True)

W, H = 1080, 1920
API = "https://commons.wikimedia.org/w/api.php"
HEADERS = {"User-Agent": "ShortsGenerator/1.0"}

NICHES = [
    "ciencia cotidiana",
    "historia sorprendente",
    "tecnología",
    "animales sorprendentes",
    "curiosidades del mundo"
]


def generate_script():
    key = os.environ["GEMINI_API_KEY"]
    niche = random.choice(NICHES)

    prompt = f"""
Crea un guion original para YouTube Shorts en español latino.
Tema: {niche}.

Devuelve exclusivamente JSON válido:
title: título atractivo de máximo 65 caracteres.
description: descripción breve.
narration: narración de 90 a 110 palabras.
scenes: exactamente 5 objetos con search_term en inglés
y caption en español.

Usa información verificable. No inventes datos.
"""

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-3.8-flash:generateContent?key=" + key
    )

    
for intento in range(3):
    try:
        r = requests.post(
            url,
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=(20, 240)
        )
        r.raise_for_status()
        break
    except (requests.exceptions.Timeout,
            requests.exceptions.ConnectionError):
        if intento == 2:
            raise
        print("Gemini tardó demasiado. Reintentando...")
        time.sleep(10 * (intento + 1))


    raw = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = re.sub(r"```(?:json)?", "", raw).strip()

    data = json.loads(raw)

    if len(data["scenes"]) != 5:
        raise ValueError("Se necesitan cinco escenas.")

    return data, niche


def find_image(term, index):
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": term,
        "gsrnamespace": 6,
        "gsrlimit": 15,
        "prop": "imageinfo",
        "iiprop": "url|extmetadata",
        "iiurlwidth": 1400
    }

    r = requests.get(
        API, params=params, headers=HEADERS, timeout=45
    )
    r.raise_for_status()

    pages = list(
        r.json().get("query", {}).get("pages", {}).values()
    )

    choices = []

    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        license_name = meta.get("LicenseShortName", {}).get("value", "")

        if info.get("thumburl") and license_name:
            choices.append((page, info, meta))

    if not choices:
        raise RuntimeError("No se encontró imagen: " + term)

    page, info, meta = random.choice(choices)

    download = requests.get(
        info["thumburl"], headers=HEADERS, timeout=60
    )
    download.raise_for_status()

    path = OUT / f"imagen{index}.jpg"
    path.write_bytes(download.content)

    def clean(value):
        return unescape(re.sub("<[^>]*>", "", str(value or "")))

    credit = {
        "archivo": page["title"],
        "autor": clean(meta.get("Artist", {}).get("value")),
        "licencia": clean(license_name),
        "fuente": info.get("descriptionurl", "")
    }

    return path, credit


async def voice(text):
    speaker = edge_tts.Communicate(
        text,
        "es-AR-TomasNeural",
        rate="+3%"
    )
    await speaker.save(str(OUT / "voz.mp3"))


def create_video(data, images):
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        58
    )

    segments = []

    for i, (path, caption) in enumerate(images):
        overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        lines = textwrap.wrap(caption, 22)
        y = H - 430 - len(lines) * 75

        draw.rounded_rectangle(
            (55, y - 25, W - 55, y + len(lines) * 75 + 25),
            radius=25,
            fill=(0, 0, 0, 195)
        )

        for n, line in enumerate(lines):
            draw.text(
                (85, y + n * 75),
                line,
                font=font,
                fill="white"
            )

        overlay_path = OUT / f"texto{i}.png"
        overlay.save(overlay_path)

        segment = OUT / f"parte{i}.mp4"

        subprocess.run([
            "ffmpeg", "-y",
            "-loop", "1", "-i", str(path),
            "-i", str(overlay_path),
            "-t", "8",
            "-filter_complex",
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},zoompan=z='min(zoom+0.0008,1.08)':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=240:s={W}x{H}:fps=30[v];[v][1:v]overlay=0:0",
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            str(segment)
        ], check=True, stdout=subprocess.DEVNULL,
           stderr=subprocess.DEVNULL)

        segments.append(segment)

    concat = OUT / "lista.txt"
    concat.write_text(
        "".join(f"file '{p.resolve()}'\n" for p in segments)
    )

    joined = OUT / "video.mp4"

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        str(joined)
    ], check=True, stdout=subprocess.DEVNULL,
       stderr=subprocess.DEVNULL)

    asyncio.run(voice(data["narration"]))

    final = OUT / "short.mp4"

    subprocess.run([
        "ffmpeg", "-y",
        "-i", str(joined),
        "-i", str(OUT / "voz.mp3"),
        "-t", "40",
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        "-movflags", "+faststart",
        str(final)
    ], check=True, stdout=subprocess.DEVNULL,
       stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    print("Iniciando generador...")

    data, niche = generate_script()
    print("Tema elegido:", niche)

    images = []
    credits = []

    for i, scene in enumerate(data["scenes"]):
        path, credit = find_image(scene["search_term"], i)
        images.append((path, scene["caption"]))
        credits.append(credit)

    create_video(data, images)

    (OUT / "metadata.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2)
    )

    (OUT / "creditos.txt").write_text(
        json.dumps(credits, ensure_ascii=False, indent=2)
    )

    print("SHORT GENERADO CORRECTAMENTE")
