"""Train the optional RandomForest presence classifier from collect.py CSVs."""

from __future__ import annotations

import argparse
import csv
import glob
import json
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split


FEATURES = ["band_energy", "peak_ratio", "variance", "spectral_entropy"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train RescueSense optional presence model")
    parser.add_argument("data", nargs="+", help="feature CSV files or glob patterns")
    parser.add_argument("--output", default="presence_model.joblib")
    parser.add_argument("--trees", type=int, default=250)
    return parser.parse_args()


def _label(row: dict[str, str]) -> int:
    if row.get("label_present", "").strip() in {"0", "1"}:
        return int(row["label_present"])
    return 1 if row.get("label", "").strip().lower() == "present" else 0


def main() -> None:
    args = parse_args()
    files = sorted({Path(path) for pattern in args.data for path in glob.glob(pattern)})
    if not files:
        raise SystemExit("No CSV files matched.")
    rows: list[list[float]] = []
    labels: list[int] = []
    for path in files:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    features = [float(row[name]) for name in FEATURES]
                except (KeyError, TypeError, ValueError):
                    continue
                if np.isfinite(features).all():
                    rows.append(features)
                    labels.append(_label(row))
    if len(rows) < 12 or len(set(labels)) < 2:
        raise SystemExit("Need at least 12 valid windows spanning both present and absent labels.")

    x = np.asarray(rows, dtype=float)
    y = np.asarray(labels, dtype=int)
    model = RandomForestClassifier(
        n_estimators=args.trees,
        min_samples_leaf=2,
        class_weight="balanced",
        random_state=7,
        n_jobs=-1,
    )
    # Tiny hackathon datasets can be evaluated without withholding so much
    # data that one class disappears; larger captures get a useful holdout.
    accuracy: float | None = None
    if len(rows) >= 30 and min(np.bincount(y)) >= 5:
        x_train, x_test, y_train, y_test = train_test_split(
            x, y, test_size=0.25, stratify=y, random_state=7
        )
        model.fit(x_train, y_train)
        accuracy = float(accuracy_score(y_test, model.predict(x_test)))
    else:
        model.fit(x, y)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, output)
    metadata = {
        "features": FEATURES,
        "rows": len(rows),
        "present_rows": int(np.sum(y == 1)),
        "absent_rows": int(np.sum(y == 0)),
        "holdout_accuracy": accuracy,
        "files": [str(path) for path in files],
    }
    output.with_suffix(output.suffix + ".json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))
    print(f"Saved model to {output}")


if __name__ == "__main__":
    main()
