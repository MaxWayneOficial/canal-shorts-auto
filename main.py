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
from io import BytesIO

import requests
from PIL import Image, ImageDraw, ImageFont
import edge_tts

# ---------------- CONFIGURACION ----------------
OUT = Path("output")
OUT.mkdir(exist_ok=True)
WIDTH, HEIGHT = 1080, 1920
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
HEADERS = {
    "User-Agent": "CanalShortsAuto/1.0 (automated educational Shorts generator)"
}
GEMINI_MODEL = "gemini-3.8-flash"
VOICE = "es-AR-TomasNeural"

NICHES = [
    "ciencia cotidiana",
    "historia sorprendente",
    "tecnologia",
    "animales sorprendentes",
    "curiosidades del mundo",
]

STOP_WORDS = {
    "underwater", "macro", "reef", "photo", "image", "picture",
    "closeup", "close", "high", "quality", "background", "cinematic",
    "beautiful", "detailed", "footage", "stock", "video", "the", "and",
    "with", "for", "from",
}


def get_with_retries(url, params=None, headers=None, timeout=(15, 60), attempts=3):
    """GET con reintentos ante cortes temporales o errores 429/5xx."""
    last_error = None
    for attempt in range(attempts):
        try:
            response = requests.get(
                url,
                params=params,
                headers=headers or HEADERS,
                timeout=timeout,
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == attempts - 1:
                    response.raise_for_status()
                wait = min(5 * (2 ** attempt), 30)
                print(f"Servidor HTTP {response.status_code}; reintentando en {wait}s...")
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                break
            wait = min(5 * (2 ** attempt), 30)
            print(f"Problema de conexión; reintentando en {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"No se pudo conectar con {url}: {last_error}")


def generate_script():
    """Genera tema, guion y cinco escenas con Gemini."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Falta el secreto GEMINI_API_KEY. Crealo en GitHub > Settings > Secrets and variables > Actions."
        )

    niche = random.choice(NICHES)
    prompt = f'''
Escribe un guion ORIGINAL para un YouTube Short en español latino.
Temática: {niche}.

Devuelve SOLAMENTE un objeto JSON válido, sin Markdown ni texto adicional,
con esta estructura:
{{
  "title": "título atractivo de máximo 65 caracteres",
  "description": "descripción breve",
  "narration": "narración natural de 90 a 110 palabras",
  "scenes": [
    {{
      "search_term": "búsqueda visual concreta en inglés de 2 a 4 palabras",
      "caption": "frase corta en español para pantalla"
    }}
  ]
}}

Incluye exactamente 5 escenas. Los términos visuales deben ser simples y
concretos (por ejemplo: "mantis shrimp", "ocean shrimp", "animal eyes").
Usa hechos verificables. No inventes cifras, fechas ni datos.
No incluyas consejos peligrosos ni afirmaciones médicas.
'''

    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "responseMimeType": "application/json",
        },
    }

    response = None
    for attempt in range(4):
        try:
            response = requests.post(
                endpoint,
                headers={"x-goog-api-key": api_key},
                json=payload,
                timeout=(20, 150),
            )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 3:
                    raise RuntimeError(
                        f"Gemini devolvió HTTP {response.status_code}: {response.text[:2000]}"
                    )
                wait = min(15 * (2 ** attempt), 60)
                print(
                    f"Gemini devolvió HTTP {response.status_code}; "
                    f"reintento {attempt + 2}/4 en {wait}s..."
                )
                time.sleep(wait)
                continue
            if not response.ok:
                raise RuntimeError(
                    f"Error de Gemini HTTP {response.status_code}: {response.text[:2500]}"
                )
            break
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt == 3:
                raise RuntimeError(
                    f"Gemini no respondió después de 4 intentos: {exc}"
                ) from exc
            wait = min(10 * (2 ** attempt), 60)
            print(f"Gemini no respondió; reintento {attempt + 2}/4 en {wait}s...")
            time.sleep(wait)

    if response is None or not response.ok:
        raise RuntimeError("Gemini no entregó una respuesta válida.")

    result = response.json()
    try:
        raw = result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            "Gemini respondió, pero no incluyó el texto del guion: "
            + json.dumps(result, ensure_ascii=False)[:2000]
        ) from exc

    raw = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", raw.strip(), flags=re.IGNORECASE)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Gemini no devolvió JSON válido ({exc}). Respuesta: {raw[:1500]}"
        ) from exc

    required = ("title", "description", "narration", "scenes")
    missing = [field for field in required if field not in data]
    if missing:
        raise ValueError(f"Faltan campos en el guion: {', '.join(missing)}")
    if not isinstance(data["scenes"], list) or len(data["scenes"]) != 5:
        raise ValueError("Gemini debe devolver exactamente cinco escenas.")
    if not isinstance(data["narration"], str) or not data["narration"].strip():
        raise ValueError("La narración está vacía o tiene formato incorrecto.")

    for number, scene in enumerate(data["scenes"], start=1):
        if not isinstance(scene, dict):
            raise ValueError(f"La escena {number} no es un objeto JSON.")
        if not scene.get("search_term") or not scene.get("caption"):
            raise ValueError(f"La escena {number} no tiene búsqueda o texto.")

    return data, niche


def build_search_terms(term):
    """Prepara búsquedas alternativas cuando una consulta es demasiado específica."""
    words = re.findall(r"[a-zA-Z]{3,}", term.lower())
    useful = [word for word in words if word not in STOP_WORDS]
    terms = [term.strip()]
    if useful:
        terms.append(" ".join(useful))
        for index in range(len(useful) - 1):
            terms.append(" ".join(useful[index:index + 2]))
        for word in reversed(useful):
            terms.append(word)
    return list(dict.fromkeys(query for query in terms if query))


def find_image(term, index):
    """Busca una imagen en Commons y conserva licencia, autor y fuente."""
    selected = None
    for query in build_search_terms(term):
        print(f"Buscando imagen {index + 1}/5: {query}")
        params = {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": 6,
            "gsrlimit": 30,
            "prop": "imageinfo",
            # mime es obligatorio aquí: el código filtra por image/*.
            "iiprop": "url|extmetadata|mime",
            "iiurlwidth": 1400,
            "iiextmetadatalanguage": "en",
        }
        response = get_with_retries(
            COMMONS_API,
            params=params,
            headers=HEADERS,
            timeout=(15, 60),
        )
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(
                "Error de Wikimedia Commons: "
                + json.dumps(payload["error"], ensure_ascii=False)[:1200]
            )

        pages = list(payload.get("query", {}).get("pages", {}).values())
        candidates = []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            metadata = info.get("extmetadata", {})
            mime_type = info.get("mime", "")
            media_url = info.get("thumburl") or info.get("url")
            license_name = (
                metadata.get("LicenseShortName", {}).get("value", "")
                or metadata.get("UsageTerms", {}).get("value", "")
            )
            if media_url and mime_type.startswith("image/") and license_name:
                candidates.append((page, info, metadata, license_name))

        if candidates:
            selected = random.choice(candidates)
            print(f"Imagen encontrada mediante: {query}")
            break
        print(f"Sin imágenes con licencia identificable para: {query}")

    if selected is None:
        raise RuntimeError(
            f"No se encontró una imagen con licencia para '{term}'. "
            f"Consultas probadas: {build_search_terms(term)}"
        )

    page, info, metadata, license_name = selected
    media_url = info.get("thumburl") or info["url"]
    download = get_with_retries(
        media_url,
        headers=HEADERS,
        timeout=(15, 90),
    )

    try:
        image = Image.open(BytesIO(download.content))
        image.load()
        image = image.convert("RGB")
    except Exception as exc:
        raise RuntimeError(
            f"La descarga de Wikimedia no era una imagen válida para '{term}': {exc}"
        ) from exc

    image_path = OUT / f"imagen{index}.jpg"
    image.save(image_path, format="JPEG", quality=92, optimize=True)

    def clean(value):
        return unescape(re.sub(r"<[^>]*>", "", str(value or ""))).strip()

    credit = {
        "archivo": page.get("title", ""),
        "autor": clean(metadata.get("Artist", {}).get("value", "")),
        "licencia": clean(license_name),
        "fuente": info.get("descriptionurl", "https://commons.wikimedia.org/"),
    }
    return image_path, credit


async def generate_voice(text):
    """Genera narración en español argentino con reintentos."""
    last_error = None
    for attempt in range(3):
        try:
            communicator = edge_tts.Communicate(text, VOICE, rate="+3%")
            await communicator.save(str(OUT / "voz.mp3"))
            voice_file = OUT / "voz.mp3"
            if not voice_file.exists() or voice_file.stat().st_size < 1000:
                raise RuntimeError("Edge TTS devolvió un archivo de audio vacío.")
            return
        except Exception as exc:
            last_error = exc
            if attempt == 2:
                break
            wait = 5 * (attempt + 1)
            print(f"Error de voz; reintentando en {wait}s: {exc}")
            await asyncio.sleep(wait)
    raise RuntimeError(f"No se pudo generar la voz con Edge TTS: {last_error}")


def run_ffmpeg(command, stage):
    """Ejecuta FFmpeg y muestra las últimas líneas del error si falla."""
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "Sin detalles")[-5000:]
        raise RuntimeError(
            f"FFmpeg falló durante '{stage}' (código {result.returncode}):\n{details}"
        )


def create_video(data, images):
    """Crea el video vertical, une las escenas y agrega la voz."""
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    if not Path(font_path).exists():
        raise RuntimeError(f"No se encontró la fuente: {font_path}")
    font = ImageFont.truetype(font_path, 58)
    segments = []

    for index, (image_path, caption) in enumerate(images):
        print(f"Creando escena {index + 1}/5...")
        overlay = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        lines = textwrap.wrap(caption, 22) or [caption]
        y = HEIGHT - 430 - len(lines) * 75

        draw.rounded_rectangle(
            (55, y - 25, WIDTH - 55, y + len(lines) * 75 + 25),
            radius=25,
            fill=(0, 0, 0, 195),
        )
        for line_number, line in enumerate(lines):
            draw.text(
                (85, y + line_number * 75),
                line,
                font=font,
                fill="white",
                stroke_width=2,
                stroke_fill="black",
            )

        overlay_path = OUT / f"texto{index}.png"
        overlay.save(overlay_path)
        segment_path = OUT / f"parte{index}.mp4"

        filter_complex = (
            f"[0:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},"
            f"zoompan=z='min(zoom+0.0008,1.08)':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d=240:s={WIDTH}x{HEIGHT}:fps=30[v];"
            f"[v][1:v]overlay=0:0,format=yuv420p[out]"
        )

        run_ffmpeg(
            [
                "ffmpeg", "-y",
                "-loop", "1", "-framerate", "30", "-i", str(image_path),
                "-i", str(overlay_path),
                "-filter_complex", filter_complex,
                "-map", "[out]", "-t", "8", "-an",
                "-c:v", "libx264", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-r", "30",
                str(segment_path),
            ],
            f"escena {index + 1}",
        )
        segments.append(segment_path)

    concat_file = OUT / "lista.txt"
    concat_file.write_text(
        "".join(f"file '{segment.resolve()}'\n" for segment in segments),
        encoding="utf-8",
    )
    joined_video = OUT / "video_sin_audio.mp4"

    run_ffmpeg(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_file),
            "-c:v", "libx264", "-preset", "veryfast",
            "-pix_fmt", "yuv420p", "-r", "30",
            str(joined_video),
        ],
        "unión de escenas",
    )
    if not joined_video.exists() or joined_video.stat().st_size < 10000:
        raise RuntimeError("FFmpeg no generó el video intermedio.")

    print("Generando narración...")
    asyncio.run(generate_voice(data["narration"]))
    final_video = OUT / "short.mp4"

    run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-i", str(joined_video),
            "-i", str(OUT / "voz.mp3"),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart",
            str(final_video),
        ],
        "mezcla de video y voz",
    )
    if not final_video.exists() or final_video.stat().st_size < 10000:
        raise RuntimeError("No se pudo crear short.mp4.")
    return final_video


if __name__ == "__main__":
    try:
        print("=== GENERADOR DE YOUTUBE SHORTS ===")
        data, niche = generate_script()
        print("Temática elegida:", niche)
        print("Título:", data["title"])

        images = []
        credits = []
        for index, scene in enumerate(data["scenes"]):
            image_path, credit = find_image(scene["search_term"], index)
            images.append((image_path, scene["caption"]))
            credits.append(credit)

        print("Todas las imágenes están listas.")
        final_video = create_video(data, images)

        metadata = {
            "niche": niche,
            **data,
            "media_credits": credits,
        }
        (OUT / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        attribution = ["Créditos de imágenes de Wikimedia Commons:"]
        for item in credits:
            attribution.append(
                f"- {item['archivo']} | Autor: {item['autor']} | "
                f"Licencia: {item['licencia']} | Fuente: {item['fuente']}"
            )
        (OUT / "creditos.txt").write_text(
            "\n".join(attribution),
            encoding="utf-8",
        )

        print("===================================")
        print("SHORT GENERADO CORRECTAMENTE")
        print("Archivo:", final_video)
        print("===================================")

    except Exception as error:
        print("===================================")
        print("ERROR DURANTE LA GENERACIÓN")
        print(type(error).__name__ + ":", str(error))
        print("===================================")
        raise
