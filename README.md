# SplitSense

SplitSense is an expense-settling agent for roommates and groups.
You describe expenses in plain English, and it returns:

1. A structured expense summary.
2. A minimum-transaction settlement plan.
3. Friendly reminder messages ready to send.

## Agentic 4-step pipeline

SplitSense now uses a clear multi-step flow instead of one combined LLM call:

1. **Step 1 (LLM parse only)**
   - Parses raw text into `expense_summary` entries only:
     `description`, `paid_by`, `amount`, `split_between`.
   - If payer/amount are present but split participants are unclear,
     it returns a friendly clarification error instead of guessing.

2. **Step 2 (pure Python compute)**
   - Computes per-person net balances with real arithmetic.
   - Runs greedy debt simplification (max-creditor vs max-debtor) to produce
     the settlement plan.

3. **Step 3 (Python verification)**
   - Verifies the settlement mathematically against net balances and transfer
     volume from original expenses.
   - Retries compute+verify once if verification fails.

4. **Step 4 (LLM reminders only)**
   - Drafts short, friendly one-to-two-sentence reminders from the verified
     settlement plan.

During each request, backend logs print these stages in order so the flow is
visible in live demos.

## Architecture (text diagram)

```text
User text input
   |
   v
[Step 1: LLM parser via OpenRouter]
   |  -> structured expense_summary OR clarification error
   v
[Step 2: Python compute_balances + compute_settlement]
   |
   v
[Step 3: Python verify_settlement (+ one retry on failure)]
   |
   v
[Step 4: LLM reminder drafter via OpenRouter]
   |
   v
Frontend cards: Expense summary | Settlement plan | Reminders
```

## Project structure

```text
splitsense/
|- app.py                 Flask backend + agent pipeline
|- templates/
|  |- index.html          Frontend page
|- static/
|  |- style.css           Styling
|  |- script.js           Frontend logic + progress indicator
|- .env.example           Template for API key and model override
|- requirements.txt       Python dependencies
|- README.md
```

## Setup

### 1) Install dependencies

Create and activate a virtual environment, then install requirements:

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 2) Configure OpenRouter

Create `.env` from `.env.example`, then set your key:

```bash
copy .env.example .env
```

In `.env`:

```text
OPENROUTER_API_KEY=sk-or-v1-...your-real-key...
```

Optional override (single fixed model):

```text
OPENROUTER_MODEL=meta-llama/llama-3.3-70b-instruct:free
```

If `OPENROUTER_MODEL` is not set, SplitSense automatically tries a fallback
list of free models in order.

### 3) Run

```bash
python app.py
```

Open http://localhost:5000

## Usage note (important)

For each expense, explicitly name everyone in that split, not only the payer.

Example:
`Raj paid 4500 for hotel, split between Raj, Simran, and Aman. Simran paid 900 for lunch, split between Simran and Aman.`

## Production-quality safeguards retained

- OpenRouter integration with free-model fallback list.
- `OPENROUTER_MODEL` single-model override support.
- Friendly handling for missing API key, auth errors, rate limits, network
  errors, timeouts, model unavailability, and upstream failures.
- Pre-LLM input validation for empty/gibberish-like text.
- JSON extraction/parsing retry logic and response-shape validation for LLM
  steps.
- Clean card-based frontend with loading spinner and explicit 4-step progress
  indicator during processing.
