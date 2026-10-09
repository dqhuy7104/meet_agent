# VIVOS Vietnamese Speech Corpus

## Description

VIVOS is a Vietnamese speech dataset for **Automatic Speech Recognition (ASR)**.

Main purposes:
- Train / fine-tune Vietnamese ASR models
- Evaluate speech-to-text performance
- Test Vietnamese audio preprocessing and ASR pipelines

Source: `kynthesis/vivos-vietnamese-speech-corpus-for-asr`

Download:

```python
import kagglehub

path = kagglehub.dataset_download(
    "kynthesis/vivos-vietnamese-speech-corpus-for-asr"
)
```

---

## Folder Structure

```text
vivos/
├── train/
│   ├── waves/          # Training audio
│   └── prompts.txt     # Training transcripts
│
└── test/
    ├── waves/          # Test audio
    └── prompts.txt     # Test transcripts
```

Expected sample representation:

```python
{
    "audio_path": "path/to/audio.wav",
    "text": "Vietnamese transcription"
}
```

---

## Metadata

| Field | Description |
|---|---|
| `audio_path` | Path to speech audio |
| `text` | Ground-truth Vietnamese transcript |
| `split` | `train` or `test` |
| `speaker_id` | Speaker identifier if available |

Recommended normalized format:

```json
{
    "audio_path": "train/waves/VIVOSSPK01/xxx.wav",
    "text": "nội dung tiếng Việt",
    "speaker_id": "VIVOSSPK01",
    "split": "train"
}
```

Audio should be normalized to:

```text
Sample rate: 16 kHz
Channels: Mono
Format: WAV
```

---

## Evaluation

Primary ASR metrics:

### Word Error Rate (WER)

```text
WER = (Substitutions + Deletions + Insertions) / Reference Words
```

### Character Error Rate (CER)

```text
CER = (Substitutions + Deletions + Insertions) / Reference Characters
```

Lower scores indicate better ASR performance.

Recommended reporting:

```text
Model:
Dataset: VIVOS
Split: test
WER:
CER:
```