import kagglehub
from pathlib import Path

# Thư mục chứa file Python hiện tại
DATASET_DIR = Path(__file__).resolve().parent / "vivos"

path = kagglehub.dataset_download(
    "kynthesis/vivos-vietnamese-speech-corpus-for-asr",
    output_dir=str(DATASET_DIR)
)

print("Path to dataset files:", path)