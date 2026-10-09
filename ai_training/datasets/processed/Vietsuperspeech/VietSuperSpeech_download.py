from huggingface_hub import snapshot_download
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parent / "VietSuperSpeech"

path = snapshot_download(
    repo_id="thanhnew2001/VietSuperSpeech",
    repo_type="dataset",
    local_dir=DATASET_DIR,
)

print("Dataset directory:", path)