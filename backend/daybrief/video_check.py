"""Offline container preflight; never synthesize speech or display credentials."""

from .config import Settings
from .video import preflight


def main():
    from azure.cognitiveservices import speech

    # Construction loads native SDK libraries without contacting Azure.
    speech.SpeechConfig(subscription="offline-check", region="westus2")
    preflight(Settings(_env_file=None))
    print("Video dependencies ready: Speech SDK, Pillow, FFmpeg/ffprobe, font.")


if __name__ == "__main__":
    main()
