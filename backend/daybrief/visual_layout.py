"""16:9 previews and restrained silent-scene FFmpeg preparation."""

import io
import math

from PIL import Image, ImageOps

from .visuals import asset_path


def preview_scene(scene, root):
    with Image.open(asset_path(root, scene.anchor_asset.image_path)) as source:
        anchor = ImageOps.fit(source.convert("RGB"), (1280, 720))
    if scene.article_image:
        canvas = Image.new("RGB", (1280, 720), "#172536")
        with Image.open(asset_path(root, scene.article_image.local_path)) as source:
            article = ImageOps.contain(source.convert("RGB"), (1280, 720))
        canvas.paste(article, ((1280 - article.width) // 2, (720 - article.height) // 2))
        canvas.paste(anchor.resize((320, 180)), (928, 472))
    else:
        canvas = anchor
    output = io.BytesIO()
    canvas.save(output, "JPEG", quality=90)
    return output.getvalue()


def scene_ffmpeg_args(scene, root, output):
    """Return argv only. No audio synthesis or final video assembly is performed."""
    frames = math.ceil(scene.duration_seconds * 30)
    anchor = str(asset_path(root, scene.anchor_asset.image_path))
    primary = str(asset_path(root, scene.article_image.local_path)) if scene.article_image else anchor
    args = ["ffmpeg", "-y", "-i", primary]
    zoom = "1.005+0.005*sin(on/30)" if scene.motion == "anchor_breathe" else f"1+0.02*on/{frames}"
    x = f"(iw-iw/zoom)*on/{frames}" if scene.motion == "gentle_pan" else "iw/2-iw/zoom/2"
    graph = (
        "[0:v]scale=1280:720:force_original_aspect_ratio=decrease,"
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=0x172536,setsar=1,"
        f"zoompan=z='{zoom}':x='{x}':y='ih/2-ih/zoom/2':d={frames}:s=1280x720:fps=30[main]"
    )
    if scene.article_image:
        args += ["-i", anchor]
        graph += (
            f";[1:v]scale=320:180,zoompan=z='1.003':d={frames}:s=320x180:fps=30[pip]"
            ";[main][pip]overlay=928:472:shortest=1[out]"
        )
    else:
        graph += ";[main]null[out]"
    return args + ["-filter_complex", graph, "-map", "[out]", "-t", str(scene.duration_seconds),
                   "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output)]


def crossfade_filter(offset, duration=0.25):
    """For future assembly; overlap must be accounted for in the audio timeline."""
    if not math.isfinite(offset) or not math.isfinite(duration) or offset < 0 or not 0 < duration <= 0.5:
        raise ValueError("Invalid restrained crossfade timing")
    return f"xfade=transition=fade:duration={duration}:offset={offset}"
