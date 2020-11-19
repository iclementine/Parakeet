from paddle.io import Dataset
from pathlib import Path

class LJSpeechMetaData(Dataset):
    def __init__(self, root):
        self.root = Path(root).expanduser()
        wav_dir = self.root / "wavs"
        csv_path = self.root / "metadata.csv"
        records = []
        speaker_name = "ljspeech"
        with open(str(csv_path), 'rt') as f:
            for line in f:
                filename, _, normalized_text = line.strip().split("|")
                filename = str(wav_dir / (filename + ".wav"))
                records.append([filename, normalized_text, speaker_name])
        self.records = records

    def __getitem__(self, i):
        return self.records[i]

    def __len__(self):
        return len(self.records)
