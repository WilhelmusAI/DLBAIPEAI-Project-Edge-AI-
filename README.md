# DLBAIPEAI – Project Edge AI

**IU International University – DLBAIPEAI Project Edge AI**  
**Task 2: Age, Gender and Expression Recognition Application**  
**Student: W. Pretorius**

This repository contains the complete practical implementation of an on-device Android application that detects a face and estimates:

- **Age group** — Adult or Elderly
- **Gender label** — Female or Male
- **Facial expression** — Angry, Disgust, Fear, Happy, Sad, Surprise or Neutral

The final Android application runs the trained models locally on the phone. Camera images and selected gallery images are processed on-device and are not sent to a remote inference server.

---

## Quick review

For the fastest review of the project:

1. **Install the Android application**  
   [`EdgeFace.apk`](./EdgeFace.apk)

2. **Review the age model**  
   [`Easyview Age Model`](./Easyview%20Age%20Model)

3. **Review the gender model**  
   [`Easyview Gender Model`](./Easyview%20Gender%20Model)

4. **Review the expression model**  
   [`Easyview Expression Model`](./Easyview%20Expression%20Model)

5. **Review the Android application builder/source**  
   [`Build_EdgeFace_Complete.py`](./Build_EdgeFace_Complete.py)

The Easyview folders contain PDF versions of the model work together with the executed Jupyter notebooks so that the training process, evaluation and outputs can be inspected without rerunning the models.

---

## Android application – EdgeFace

The final application combines all three trained models into one Android application.

### Main features

- Take a photograph using the phone camera
- Select an existing image from the gallery
- Browse for an image file on the device
- Automatic face detection
- Display the exact face crop supplied to the models
- Run all three models on the device
- Display model scores and inference times
- Run an on-device CPU benchmark
- Export benchmark results
- Works without cloud inference

The Android application uses the same **224 × 224 RGB face input** used by the trained models. Pixel values remain in the **0–255 range**, with input preprocessing contained inside the TensorFlow Lite models.

### Android compatibility

- Minimum Android version: **Android 8.0 / API 26**
- On-device inference: **TensorFlow Lite / LiteRT**
- Face detection: **Google ML Kit**
- Camera integration: **AndroidX CameraX**
- Model execution: **CPU**
- Network connection is not required for model inference

---

## Models

### 1. Age model

**Architecture:** MobileNetV3-Small  
**Dataset:** FairFace

The age model is a two-class classifier:

| Class | Project definition |
|---|---|
| Adult | 20–59 years |
| Elderly | 60+ years |

Images below age 20 were excluded from training for this classifier.

Files for quick inspection are available in:

[`Easyview Age Model`](./Easyview%20Age%20Model)

The full training/export archive is stored as:

[`age_results.zip`](./age_results.zip)

---

### 2. Gender model

**Architecture:** MobileNetV3-Small  
**Dataset:** FairFace

The model predicts the two labels supplied by FairFace:

- Female
- Male

These labels refer to the dataset's visual classification labels and should not be interpreted as determining a person's gender identity.

Files for quick inspection are available in:

[`Easyview Gender Model`](./Easyview%20Gender%20Model)

The full training/export archive is stored as:

[`gender_results.zip`](./gender_results.zip)

---

### 3. Expression model

**Architecture:** EfficientNetV2-B0  
**Dataset:** Expression in-the-Wild (ExpW)

The expression model predicts seven classes:

1. Angry
2. Disgust
3. Fear
4. Happy
5. Sad
6. Surprise
7. Neutral

The model was trained using a group-aware train/validation/holdout split so that faces originating from the same source photograph do not cross between the data splits.

Files for quick inspection are available in:

[`Easyview Expression Model`](./Easyview%20Expression%20Model)

The complete original and recovery archives are stored as:

- [`expression_original.zip`](./expression_original.zip)
- [`expression_recovery.zip`](./expression_recovery.zip)

The recovery files are retained to document the recovery of the completed training run after the Colab runtime ended.

---

## Measured Android benchmark

The application contains a benchmark that measures the TensorFlow Lite interpreter on the phone.

Example measurement on a **Samsung SM-S921B, Android 15, arm64-v8a**, using **2 CPU threads**:

| Model | Median inference | p95 inference |
|---|---:|---:|
| Age | 2.1 ms | 2.1 ms |
| Gender | 2.2 ms | 2.3 ms |
| Expression | 25.6 ms | 26.2 ms |

These values measure model invocation only. Complete analysis also includes image decoding, face detection, cropping and resizing, so total end-to-end processing time is higher.

---

## Final 20-image evaluation

The final device evaluation contains **20 images** collected according to the Task 2 requirements.

The final results are organised into six easy-to-review categories:

```text
Final Results/
├── Adult/
├── Elderly/
├── Male/
├── Female/
├── Happy/
└── Sad/
```

Each test image belongs to:

- one age category,
- one gender-label category, and
- one expression category.

The final set is balanced according to the required Adult/Elderly, Male/Female and Happy/Sad evaluation structure.

---

## Repository structure

```text
DLBAIPEAI-Project-Edge-AI-/
│
├── Build_EdgeFace_Complete.py
├── EdgeFace.apk
│
├── Easyview Age Model/
│   ├── 01_Age_Model_Colab.pdf
│   └── 01_Age_Model_Colab_executed.ipynb
│
├── Easyview Gender Model/
│   ├── 02_Gender_Model_Colab.pdf
│   └── 02_Gender_Model_Colab_executed.ipynb
│
├── Easyview Expression Model/
│   ├── 03_Expression_Model_Colab.pdf
│   ├── Expression Model original.ipynb
│   └── Expression Model Colab with error executed.ipynb
│
├── age_results.zip
├── gender_results.zip
├── expression_original.zip
├── expression_recovery.zip
│
└── Final Results/
    ├── Adult/
    ├── Elderly/
    ├── Male/
    ├── Female/
    ├── Happy/
    └── Sad/
```

---

## Building the Android application

The repository includes one Python builder:

```text
Build_EdgeFace_Complete.py
```

The model archive paths and project paths are defined near the top of the script.

After setting those paths to the local project folder, run:

```bash
python Build_EdgeFace_Complete.py
```

The builder:

1. verifies the three approved model archives,
2. generates the Android project,
3. prepares the required Android build tools,
4. runs the application tests,
5. builds the release APK,
6. signs the APK,
7. verifies the APK signature and model files, and
8. produces the final `EdgeFace.apk`.

The signing key is kept outside the repository and is not included here.

---

## Large files

The model result archives and APK are stored with **Git LFS**.

When cloning the full repository, Git LFS should be installed:

```bash
git lfs install
git clone https://github.com/WilhelmusAI/DLBAIPEAI-Project-Edge-AI-.git
cd DLBAIPEAI-Project-Edge-AI-
git lfs pull
```

---

## Important interpretation notes

The application is an academic demonstration of edge AI.

- The **age model** predicts only the project classes Adult and Elderly. It does not estimate an exact age.
- The **gender model** reproduces the Female/Male visual labels present in FairFace. It does not determine gender identity.
- The **expression model** predicts visible facial-expression classes. It does not determine a person's internal emotional state.
- Model scores are model outputs and should not be treated as guarantees.
- The application is not intended for medical, employment, access-control or identity decisions.

---

## Technologies used

- Python
- TensorFlow / Keras
- TensorFlow Lite / LiteRT
- Google Colab
- Kotlin
- Android Jetpack Compose
- AndroidX CameraX
- Google ML Kit Face Detection
- FairFace
- Expression in-the-Wild (ExpW)
- Git LFS

---

## Project status

- [x] Age model trained and evaluated
- [x] Gender model trained and evaluated
- [x] Expression model trained and evaluated
- [x] Models converted for Android
- [x] Android application implemented
- [x] Signed Android APK generated
- [x] Camera input tested
- [x] Gallery/file input tested
- [x] On-device benchmark completed
- [ ] Final 20-image evaluation files added to the repository

---

## Author

**W. Pretorius**  
DLBAIPEAI – Project Edge AI  
IU International University
