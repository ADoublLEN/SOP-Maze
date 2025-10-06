# SOP-Maze

**SOP-Maze** is a benchmark designed to evaluate the comprehensive capabilities of large language models (LLMs) in executing tasks that follow Standard Operating Procedures (SOPs).

## 🧩 Overview

SOP-Maze presents complex, structured tasks that mimic real-world procedural workflows. It tests an LLM's ability to:

- Understand and follow SOPs.
- Reason through multi-step operations.
- Produce accurate, context-aware outputs.

## 📁 Directory Structure

```

.
├── raw_data/                  # Original data samples (JSON)
├── data_with_model_response/ # Populated with model-augmented samples
├── quick_start.py            # Script to run evaluation

````

## 🛠️ Setup Instructions

### 1. Prepare the Data

Before evaluation, enrich each JSON file in `raw_data/` by adding a new key:

```json
"model_response": "<response_generated_by_model>"
````

* Copy the updated files into the `data_with_model_response/` directory.
* **Important:** Make sure to **clear** the `data_with_model_response/` directory before copying in new files.

You can refer to the examples already in `data_with_model_response/` for formatting guidance.

### 2. Run Evaluation

To begin evaluation, run:

```bash
sh quick_start.py
```

This will execute the evaluation pipeline on the updated dataset.

---