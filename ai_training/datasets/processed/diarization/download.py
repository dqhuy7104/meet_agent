from huggingface_hub import snapshot_download
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parent / "ViYT-Diar"

path = snapshot_download(
    repo_id="tuanduy1612/ViYT-Diar",
    repo_type="dataset",
    local_dir=DATASET_DIR,
)

print("Dataset directory:", path)