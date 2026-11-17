
import os
import json
import random
import subprocess
import asyncio
import textwrap
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
import edge_tts

OUT = Path("output")
OUT.mkdir(exist_ok=True)

WIDTH, HEIGHT = 1080, 1920

NICHES = [
    "ciencia cotidiana",
    "historia sorprendente",
    "tecnología",
    "naturaleza y animales",
    "curiosidades del mundo"
]


def generate_script():
    key = os.environ["GEMINI_API_KEY"]

    niche = random.choice(NICHES)

    prompt = f"""
    Crea un guion original para un YouTube Short en español latino.
    Tema: {niche}.

    Devuelve únicamente JSON válido con estos campos:
    title: título atractivo de máximo 65 caracteres.
    description: descripción breve.
    narration: narración de entre 90 y 110 palabras.
    scenes: exactamente 5 objetos con:
      search_term: búsqueda visual en inglés.
      caption: frase breve en español.

    Usa información verificable. No inventes estadísticas,
    fechas ni datos científicos.
    """

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-3.8-flash:generateContent?key="+ key
    )

    response = requests.post(
        url,
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=90
    )
    response.raise_for_status()

    raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
    raw = raw.strip().replace("```json", "").replace("```", "")

    data = json.loads(raw)

    if len(data["scenes"]) != 5:
        raise ValueError("El guion no contiene cinco escenas.")

    return data, niche


def download_clips(scenes):
    api_key = os.environ["PEXELS_API_KEY"]
    clips = []

    for index, scene in enumerate(scenes):
        response = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": api_key},
            params={
                "query": scene["search_term"],
                "orientation": "portrait",
                "per_page": 8
            },
            timeout=45
        )
        response.raise_for_status()

        videos = response.json().get("videos", [])

        if not videos:
            raise RuntimeError(
                "No se encontraron videos para: "
                + scene["search_term"]
            )

        video = random.choice(videos)
        video_file = max(
            video["video_files"],
            key=lambda item: item.get("height", 0)
        )

        path = OUT / f"clip{index}.mp4"

        download = requests.get(
            video_file["link"],
            timeout=90
        )
        download.raise_for_status()
        path.write_bytes(download.content)

        clips.append((path, scene["caption"]))

    return clips


async def generate_voice(text):
    voice = edge_tts.Communicate(
        text,
        "es-AR-TomasNeural",
        rate="+3%"
    )

    await voice.save(str(OUT / "voice.mp3"))


def create_video(data, clips):
    font = ImageFont.truetype(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        58
    )

    segments = []

    for index, (clip, caption) in enumerate(clips):
        image = Image.new(
            "RGBA",
            (WIDTH, HEIGHT),
            (0, 0, 0, 0)
        )

        draw = ImageDraw.Draw(image)
        lines = textwrap.wrap(caption, 22)

        y = HEIGHT - 420 - len(lines) * 72

        draw.rounded_rectangle(
            (55, y - 25, WIDTH - 55, y + len(lines) * 72 + 20),
            radius=25,
            fill=(0, 0, 0, 185)
        )

        for line_number, line in enumerate(lines):
            draw.text(
                (85, y + line_number * 72),
                line,
                font=font,
                fill="white"
            )

        overlay = OUT / f"caption{index}.png"
        image.save(overlay)

        segment = OUT / f"segment{index}.mp4"

        subprocess.run([
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", str(clip),
            "-i", str(overlay),
            "-t", "8",
            "-filter_complex",
            f"[0:v]scale={WIDTH}:{HEIGHT}:"
            "force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT}[v];"
            "[v][1:v]overlay=0:0",
            "-an",
            "-r", "30",
            "-pix_fmt", "yuv420p",
            str(segment)
        ], check=True, stdout=subprocess.DEVNULL,
           stderr=subprocess.DEVNULL)

        segments.append(segment)

    concat_file = OUT / "concat.txt"

    concat_file.write_text(
        "".join(
            "file '" + str(segment.resolve()) + "'\n"
            for segment in segments
        )
    )

    joined = OUT / "joined.mp4"

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        str(joined)
    ], check=True, stdout=subprocess.DEVNULL,
       stderr=subprocess.DEVNULL)

    asyncio.run(generate_voice(data["narration"]))

    final_video = OUT / "short.mp4"

    subprocess.run([
        "ffmpeg", "-y",
        "-i", str(joined),
        "-i", str(OUT / "voice.mp3"),
        "-t", "40",
        "-vf", "fps=30",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        "-movflags", "+faststart",
        str(final_video)
    ], check=True, stdout=subprocess.DEVNULL,
       stderr=subprocess.DEVNULL)

    return final_video


if __name__ == "__main__":
    print("Iniciando generador de Shorts...")

    data, niche = generate_script()

    print("Temática elegida:", niche)
    print("Título:", data["title"])

    clips = download_clips(data["scenes"])

    create_video(data, clips)

    metadata = {
        "niche": niche,
        **data
    }

    (OUT / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2)
    )

    print("Short generado correctamente.")
