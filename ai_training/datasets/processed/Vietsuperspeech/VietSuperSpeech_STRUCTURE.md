# VietSuperSpeech — Dataset Folder Structure

Nguồn: https://huggingface.co/datasets/thanhnew2001/VietSuperSpeech/tree/main

## 1. Cấu trúc thư mục

```text
VietSuperSpeech/
├── .gitattributes
├── README.md
├── manifest.json
├── train.json
├── dev.json
└── audio/
    ├── asr_dataset_nguoivietdailynews/
    │   └── ... (*.wav)
    ├── asr_dataset_nguoiviethaingoai/
    │   └── ... (*.wav)
    ├── asr_dataset_nguyenkhangofficial_part00/
    │   └── ... (*.wav)
    ├── asr_dataset_nguyenkhangofficial_part01/
    │   └── ... (*.wav)
    ├── asr_dataset_trinhlieu_part00/
    │   └── ... (*.wav)
    ├── asr_dataset_trinhlieu_part01/
    │   └── ... (*.wav)
    ├── asr_segments_vietcetera_part00/
    │   └── <recording_name>_segNNN.wav
    ├── asr_segments_vietcetera_part01/
    │   └── ... (*.wav)
    ├── asr_segments_vietcetera_part06/
    │   └── ... (*.wav)
    ├── asr_segments_vietcetera_part07/
    │   └── ... (*.wav)
    ├── asr_segments_vietcetera_part08/
    │   └── ... (*.wav)
    ├── asr_segments_vietcetera_part09/
    │   └── ... (*.wav)
    ├── asr_segments_vietcetera_part12/
    │   └── ... (*.wav)
    ├── asr_segments_vietcetera_part13/
    │   └── ... (*.wav)
    ├── asr_segments_vietcetera_part14/
    │   └── ... (*.wav)
    └── asr_segments_vietcetera_part15/
        └── ... (*.wav)
```

**Lưu ý:** Danh sách trên là các thư mục con hiển thị trong trình duyệt repository khi kiểm tra; không khẳng định toàn bộ file WAV đã được liệt kê. Không suy diễn các part bị thiếu số thứ tự (ví dụ `part02`) là có tồn tại.

## 2. Vai trò các file

| Đường dẫn | Vai trò |
|---|---|
| `.gitattributes` | Thiết lập Git/LFS/Xet cho repository. |
| `README.md` | Mô tả dataset, định dạng và nguồn dữ liệu; có số liệu cũ. |
| `manifest.json` | Thống kê dataset và thông tin mô hình phiên âm. |
| `train.json` | Danh sách mẫu train, tham chiếu đến WAV bằng đường dẫn tương đối. |
| `dev.json` | Danh sách mẫu development/validation. |
| `audio/` | Các file WAV, nhóm theo nguồn và/hoặc chia thành nhiều `part`. |

## 3. Schema mỗi sample

Dataset Viewer hiển thị bốn trường:

```json
{
  "audio": "audio/asr_segments_vietcetera_part00/<recording_name>_seg047.wav",
  "text": "<transcript tiếng Việt>",
  "duration": 14.8,
  "source": "<recording_name>.mp4"
}
```

- `audio`: đường dẫn tương đối tính từ thư mục gốc `VietSuperSpeech/`.
- `text`: bản phiên âm tương ứng với đoạn WAV.
- `duration`: thời lượng đoạn audio (giây).
- `source`: tên video hoặc file nguồn; không đồng nghĩa với speaker ID.

Ví dụ đường dẫn thực tế trong Dataset Viewer:

```text
audio/asr_segments_vietcetera_part00/10_nam_kham_pha_su_that_cua_nganh_Sang_tao_-_Tuan_Le_Nha_sang_lap_The_Lab_Saigon_HaveASip_24_seg047.wav
```

## 4. Thông tin thống kê

Theo `manifest.json` trên nhánh `main` khi kiểm tra:

| Trường | Giá trị |
|---|---:|
| `sample_rate` | 16.000 Hz |
| `train_samples` | 60.656 |
| `dev_samples` | 6.749 |
| `total_samples` | 67.405 |
| `total_duration_hours` | 245,42 giờ |
| `model` | Zipformer-30M-RNNT-6000h |

**Chú ý:** `README.md` vẫn ghi 32.267 samples / 103,18 giờ; ưu tiên đối chiếu `manifest.json` và dữ liệu thực tế. `manifest.json` còn liệt kê tên nhóm nguồn khác với một số thư mục nhìn thấy trong `audio/`, vì vậy không nên xem trường `datasets` là danh sách thư mục đầy đủ.

## 5. Lưu ý khi ghép WAV để test VAD/ASR/Diarization

1. Đọc `train.json` hoặc `dev.json` để lấy đúng đường dẫn `audio` và transcript `text`; tránh chỉ quét file theo tên nếu cần ground truth.
2. Gom các đoạn có cùng `source`, rồi sắp xếp theo chỉ số `_segNNN` để có thứ tự tương đối. Chỉ số segment không bảo đảm các đoạn liền kề nhau trong bản ghi gốc.
3. Nếu chèn khoảng lặng khi ghép, ghi lại `start`/`end` mới cho từng đoạn; giữ nguyên transcript tương ứng.
4. Dữ liệu này không công bố speaker ID trong schema bốn trường nói trên. Không thể suy ra ground truth diarization chỉ từ `source` hoặc tên WAV.
5. Khi cần đánh giá ASR, transcript là nhãn được tạo bằng mô hình theo README, không mặc nhiên là bản chép tay đã kiểm định.

## 6. Nguồn đối chiếu

- Repository: https://huggingface.co/datasets/thanhnew2001/VietSuperSpeech/tree/main
- Audio directory: https://huggingface.co/datasets/thanhnew2001/VietSuperSpeech/tree/main/audio
- Dataset Viewer: https://huggingface.co/datasets/thanhnew2001/VietSuperSpeech
- Manifest: https://huggingface.co/datasets/thanhnew2001/VietSuperSpeech/blob/main/manifest.json
- README: https://huggingface.co/datasets/thanhnew2001/VietSuperSpeech/blob/main/README.md
