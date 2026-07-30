

const settleBtn = document.getElementById("settle-btn");
const statusArea = document.getElementById("status-area");
const progressStepsEl = document.getElementById("progress-steps");
const resultsSection = document.getElementById("results");

const summaryList = document.getElementById("summary-list");
const settlementList = document.getElementById("settlement-list");
const remindersList = document.getElementById("reminders-list");

const personNameInput = document.getElementById("person-name-input");
const addPersonBtn = document.getElementById("add-person-btn");
const peopleChipsEl = document.getElementById("people-chips");
const peopleEmptyHintEl = document.getElementById("people-empty-hint");
const expenseRowsEl = document.getElementById("expense-rows");
const addExpenseBtn = document.getElementById("add-expense-btn");

const STEP_LABELS = ["Parsing", "Calculating", "Verifying", "Drafting reminders"];
let progressTimer = null;
let progressIndex = 0;



const formState = {
  people: [],
  expenses: [],
};

let nextExpenseId = 1;


addPersonBtn.addEventListener("click", addPersonFromInput);
personNameInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    addPersonFromInput();
  }
});

function addPersonFromInput() {
  const raw = personNameInput.value.trim();
  if (!raw) return;

  const alreadyExists = formState.people.some((p) => p.toLowerCase() === raw.toLowerCase());
  if (alreadyExists) {
    personNameInput.value = "";
    personNameInput.focus();
    return;
  }

  formState.people.push(raw);
  personNameInput.value = "";
  personNameInput.focus();
  renderPeople();
  renderExpenseRows();
}

function removePerson(name) {
  formState.people = formState.people.filter((p) => p !== name);

 
  formState.expenses.forEach((exp) => {
    exp.split.delete(name);
    if (exp.payer === name) {
      exp.payer = formState.people[0] || "";
    }
  });

  renderPeople();
  renderExpenseRows();
}

function renderPeople() {
  peopleChipsEl.innerHTML = "";

  formState.people.forEach((name) => {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.innerHTML = `<span>${escapeHtml(name)}</span>`;

    const removeBtn = document.createElement("button");
    removeBtn.className = "chip-remove";
    removeBtn.type = "button";
    removeBtn.innerHTML = "&times;";
    removeBtn.setAttribute("aria-label", `Remove ${name}`);
    removeBtn.addEventListener("click", () => removePerson(name));

    chip.appendChild(removeBtn);
    peopleChipsEl.appendChild(chip);
  });

  peopleEmptyHintEl.classList.toggle("is-hidden", formState.people.length >= 2);
  addExpenseBtn.disabled = formState.people.length < 2;
}


addExpenseBtn.addEventListener("click", addExpenseRow);

function addExpenseRow() {
  if (formState.people.length < 2) return;

  formState.expenses.push({
    id: nextExpenseId++,
    payer: formState.people[0],
    amount: "",
    description: "",
    split: new Set(formState.people),
    splitMode: "equal",
    customShares: {},
  });

  renderExpenseRows();
}

function removeExpenseRow(id) {
  formState.expenses = formState.expenses.filter((exp) => exp.id !== id);
  renderExpenseRows();
}

function renderExpenseRows() {
  expenseRowsEl.innerHTML = "";

  formState.expenses.forEach((exp) => {
    const row = document.createElement("div");
    row.className = "expense-row";

    const topLine = document.createElement("div");
    topLine.className = "expense-row-top";

    const payerSelect = document.createElement("select");
    payerSelect.className = "expense-payer";
    formState.people.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      if (name === exp.payer) opt.selected = true;
      payerSelect.appendChild(opt);
    });
    payerSelect.addEventListener("change", (e) => {
      exp.payer = e.target.value;
    });

    const paidLabel = document.createElement("span");
    paidLabel.className = "expense-static-label";
    paidLabel.textContent = "paid";

    const amountInput = document.createElement("input");
    amountInput.type = "number";
    amountInput.className = "expense-amount";
    amountInput.placeholder = "Amount";
    amountInput.min = "0";
    amountInput.step = "0.01";
    amountInput.value = exp.amount;
    amountInput.addEventListener("input", (e) => {
      exp.amount = e.target.value;
    });

    const forLabel = document.createElement("span");
    forLabel.className = "expense-static-label";
    forLabel.textContent = "for";

    const descInput = document.createElement("input");
    descInput.type = "text";
    descInput.className = "expense-desc";
    descInput.placeholder = "Description (e.g. Hotel)";
    descInput.maxLength = 80;
    descInput.value = exp.description;
    descInput.addEventListener("input", (e) => {
      exp.description = e.target.value;
    });

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "remove-expense-btn";
    removeBtn.innerHTML = "&times;";
    removeBtn.setAttribute("aria-label", "Remove this expense");
    removeBtn.addEventListener("click", () => removeExpenseRow(exp.id));

    topLine.append(payerSelect, paidLabel, amountInput, forLabel, descInput, removeBtn);

    const splitModeRow = document.createElement("div");
    splitModeRow.className = "split-mode-row";
    splitModeRow.innerHTML = `
      <label class="split-mode-option">
        <input type="radio" name="split-mode-${exp.id}" value="equal" ${exp.splitMode !== "custom" ? "checked" : ""}>
        <span>Equal split</span>
      </label>
      <label class="split-mode-option">
        <input type="radio" name="split-mode-${exp.id}" value="custom" ${exp.splitMode === "custom" ? "checked" : ""}>
        <span>Custom amounts</span>
      </label>
    `;
    splitModeRow.querySelectorAll("input").forEach((radio) => {
      radio.addEventListener("change", (e) => {
        exp.splitMode = e.target.value;
        if (!exp.customShares) exp.customShares = {};
        renderExpenseRows();
      });
    });

    const splitLine = document.createElement("div");
    splitLine.className = "expense-split";

    const splitLabel = document.createElement("span");
    splitLabel.className = "split-label";
    splitLabel.textContent = "Split between";
    splitLine.appendChild(splitLabel);

    formState.people.forEach((name) => {
      const isChecked = exp.split.has(name);

      const item = document.createElement("label");
      item.className = "split-checkbox-item" + (isChecked ? " is-checked" : "");

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = isChecked;
      checkbox.addEventListener("change", (e) => {
        if (e.target.checked) {
          exp.split.add(name);
        } else {
          exp.split.delete(name);
        }
        item.classList.toggle("is-checked", e.target.checked);
      });

      const text = document.createElement("span");
      text.textContent = name;

      item.append(checkbox, text);
      splitLine.appendChild(item);
    });

    const allBtn = document.createElement("button");
    allBtn.type = "button";
    allBtn.className = "split-all-btn";
    allBtn.textContent = "All";
    allBtn.addEventListener("click", () => {
      exp.split = new Set(formState.people);
      renderExpenseRows();
    });
    splitLine.appendChild(allBtn);

    row.appendChild(topLine);
    row.appendChild(splitModeRow);
    row.appendChild(splitLine);

    if (exp.splitMode === "custom" && exp.split.size > 0) {
      const customRow = document.createElement("div");
      customRow.className = "custom-shares-row";

      Array.from(exp.split).forEach((name) => {
        const item = document.createElement("div");
        item.className = "custom-share-item";
        item.innerHTML = `
          <span>${escapeHtml(name)}</span>
          <input type="number" min="0" step="0.01" placeholder="₹0.00" value="${exp.customShares[name] || ""}">
        `;
        item.querySelector("input").addEventListener("input", (e) => {
          exp.customShares[name] = e.target.value;
        });
        customRow.appendChild(item);
      });

      const totalHint = document.createElement("div");
      const enteredTotal = Object.values(exp.customShares).reduce((sum, v) => sum + (Number(v) || 0), 0);
      const targetAmount = Number(exp.amount) || 0;
      const isMatched = Math.abs(enteredTotal - targetAmount) < 0.01;
      totalHint.className = "custom-shares-hint" + (isMatched ? " is-matched" : "");
      totalHint.textContent = `₹${enteredTotal.toFixed(2)} of ₹${targetAmount.toFixed(2)} allocated`;
      customRow.appendChild(totalHint);

      row.appendChild(customRow);
    }

    expenseRowsEl.appendChild(row);
  });
}


function joinNamesWithAnd(names) {
  if (names.length === 1) return names[0];
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}

function validateForm() {
  if (formState.people.length < 2) {
    return "Add at least two people before settling up.";
  }
  if (formState.expenses.length === 0) {
    return "Add at least one expense before settling up.";
  }

  for (const exp of formState.expenses) {
    const amountNum = Number(exp.amount);
    if (!exp.payer) {
      return "Every expense needs someone who paid.";
    }
    if (!exp.amount || Number.isNaN(amountNum) || amountNum <= 0) {
      return "Every expense needs a valid amount greater than 0.";
    }
    if (!exp.description.trim()) {
      return "Every expense needs a short description.";
    }
    if (exp.split.size === 0) {
      return "Every expense needs at least one person in its split.";
    }

    if (exp.splitMode === "custom") {
      const enteredTotal = Object.values(exp.customShares || {}).reduce((sum, v) => sum + (Number(v) || 0), 0);
      if (Math.abs(enteredTotal - amountNum) >= 0.01) {
        return `Custom shares for "${exp.description}" must add up to the total amount.`;
      }
    }
  }

  return null;
}


function buildExpenseText() {
  return formState.expenses
    .map((exp) => {
      if (exp.splitMode === "custom" && exp.customShares) {
        const shareParts = Array.from(exp.split)
          .map((name) => `${exp.customShares[name] || 0} for ${name}`)
          .join(", ");
        return `${exp.payer} paid ${exp.amount} for ${exp.description.trim()}, split as ${shareParts}.`;
      }
      const splitNames = joinNamesWithAnd(Array.from(exp.split));
      return `${exp.payer} paid ${exp.amount} for ${exp.description.trim()}, split between ${splitNames}.`;
    })
    .join(" ");
}

settleBtn.addEventListener("click", handleSettle);

async function handleSettle() {
  const validationError = validateForm();
  if (validationError) {
    showError(validationError);
    return;
  }

  const text = buildExpenseText();

  setLoading(true);
  showSkeletonResults();

  try {
    const response = await fetch("/api/settle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });

    let data;
    try {
      data = await response.json();
    } catch (parseErr) {
      showError("The server sent back something unexpected. Please try again.");
      return;
    }

    if (!response.ok || !data.ok) {
      showError(data.error || "Something went wrong. Please try again.");
      return;
    }

    renderResults(data);
    clearStatus();
  } catch (networkErr) {
    showError("Couldn't reach the server. Please check your connection and that the app is running.");
  } finally {
    setLoading(false);
  }
}



function setLoading(isLoading) {
  settleBtn.disabled = isLoading;
  settleBtn.querySelector(".btn-label").textContent = isLoading ? "Calculating…" : "Settle it up";

  if (isLoading) {
    startProgressUI();
    statusArea.hidden = false;
    statusArea.className = "status-area loading";
    statusArea.innerHTML = `<span class="spinner"></span><span>Step 1/4: Parsing expenses...</span>`;
  } else {
    stopProgressUI();
  }
}

function showError(message) {
  stopProgressUI();
  statusArea.hidden = false;
  statusArea.className = "status-area error";
  statusArea.textContent = message;
}

function clearStatus() {
  stopProgressUI();
  statusArea.hidden = true;
  statusArea.innerHTML = "";
}

function hideResults() {
  resultsSection.hidden = true;
}

function showSkeletonResults() {
  summaryList.innerHTML = renderSkeletonItems(2);
  settlementList.innerHTML = renderSkeletonItems(2);
  remindersList.innerHTML = renderSkeletonItems(2);
  resultsSection.hidden = false;
}

function renderSkeletonItems(count) {
  let html = "";
  for (let i = 0; i < count; i++) {
    html += `
      <div class="skeleton-card">
        <div class="skeleton skeleton-line skeleton-line--medium"></div>
        <div class="skeleton skeleton-line skeleton-line--short"></div>
      </div>
    `;
  }
  return html;
}


function renderResults(data) {
  renderSummary(data.expense_summary || []);
  renderSettlement(data.settlement_plan || []);
  renderReminders(data.reminders || []);
  resultsSection.hidden = false;
  resultsSection.scrollIntoView({ behavior: "smooth", block: "nearest" });

  window.lastSettlementPlan = data.settlement_plan || [];
  window.lastExpenseSummary = data.expense_summary || [];

}

function renderSummary(items) {
  summaryList.innerHTML = "";

  if (items.length === 0) {
    summaryList.innerHTML = `<div class="empty-note">No expenses were recognized.</div>`;
    return;
  }

  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "summary-item";

    const split = Array.isArray(item.split_between) ? item.split_between.join(", ") : "";

    row.innerHTML = `
      <div class="summary-item-main">
        <div class="summary-item-desc">${escapeHtml(item.description || "Expense")}</div>
        <div class="summary-item-detail">${escapeHtml(item.paid_by || "?")} paid · split between ${escapeHtml(split)}</div>
      </div>
      <div class="summary-item-amount">${formatCurrency(item.amount)}</div>
    `;
    summaryList.appendChild(row);
  });
}

function renderSettlement(plan) {
  settlementList.innerHTML = "";

  if (plan.length === 0) {
    settlementList.innerHTML = `<div class="empty-note">Everyone's already even — no payments needed! 🎉</div>`;
    return;
  }

  plan.forEach((p) => {
    const row = document.createElement("div");
    row.className = "flow-row";
    row.dataset.txnId = p.transaction_id;
    row.innerHTML = `
      <div class="flow-name flow-name--from">${escapeHtml(p.from)}</div>
      <div class="flow-track">
        <div class="flow-dot"></div>
        <div class="flow-amount">${formatCurrency(p.amount)}</div>
      </div>
      <div class="flow-name flow-name--to">${escapeHtml(p.to)}</div>
      <button class="mark-paid-btn" data-txn-id="${p.transaction_id}">Mark as Paid</button>
    `;
    settlementList.appendChild(row);
  });

  document.querySelectorAll(".mark-paid-btn").forEach((btn) => {
    btn.addEventListener("click", () => togglePaid(btn));
  });
}

async function togglePaid(btn) {
  const txnId = btn.dataset.txnId;
  btn.disabled = true;

  try {
    const response = await fetch(`/api/mark-paid/${txnId}`, { method: "POST" });
    const data = await response.json();

    if (data.ok) {
      const row = btn.closest(".flow-row");
      if (data.is_paid) {
        row.classList.add("is-paid");
        btn.textContent = "✓ Paid";
      } else {
        row.classList.remove("is-paid");
        btn.textContent = "Mark as Paid";
      }
    }
  } catch (err) {
    console.error("Couldn't update paid status", err);
  } finally {
    btn.disabled = false;
  }
}

function downloadSettlementCSV(plan) {
  if (!plan || plan.length === 0) return;

  let csv = "From,To,Amount\n";
  plan.forEach((p) => {
    csv += `${p.from},${p.to},${p.amount}\n`;
  });

  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "settlement_plan.csv";
  a.click();
  URL.revokeObjectURL(url);
}

function renderReminders(reminders) {
  remindersList.innerHTML = "";

  if (reminders.length === 0) {
    remindersList.innerHTML = `<div class="empty-note">No reminders needed.</div>`;
    return;
  }

  reminders.forEach((text, idx) => {
    const row = document.createElement("div");
    row.className = "reminder-item";

    const textSpan = document.createElement("span");
    textSpan.className = "reminder-text";
    textSpan.textContent = text;

    const copyBtn = document.createElement("button");
    copyBtn.className = "copy-btn";
    copyBtn.textContent = "Copy";
    copyBtn.addEventListener("click", () => copyToClipboard(text, copyBtn));

    row.appendChild(textSpan);
    row.appendChild(copyBtn);
    remindersList.appendChild(row);
  });
}



function formatCurrency(amount) {
  const num = Number(amount);
  if (Number.isNaN(num)) return String(amount);
  return "₹" + num.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

async function copyToClipboard(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    const original = btn.textContent;
    btn.textContent = "Copied!";
    btn.classList.add("copied");
    setTimeout(() => {
      btn.textContent = original;
      btn.classList.remove("copied");
    }, 1500);
  } catch (err) {
    btn.textContent = "Couldn't copy";
  }
}

function startProgressUI() {
  progressIndex = 0;
  progressStepsEl.hidden = false;
  updateProgressStep(progressIndex);

  if (progressTimer) {
    clearInterval(progressTimer);
  }

  progressTimer = setInterval(() => {
    if (progressIndex < STEP_LABELS.length - 1) {
      progressIndex += 1;
      updateProgressStep(progressIndex);
    }
  }, 1100);
}

function stopProgressUI() {
  if (progressTimer) {
    clearInterval(progressTimer);
    progressTimer = null;
  }
  progressStepsEl.hidden = true;
}

function updateProgressStep(activeIndex) {
  const steps = progressStepsEl.querySelectorAll(".progress-step");

  steps.forEach((stepEl, idx) => {
    stepEl.classList.remove("is-active", "is-complete");
    if (idx < activeIndex) {
      stepEl.classList.add("is-complete");
    } else if (idx === activeIndex) {
      stepEl.classList.add("is-active");
    }
  });

  statusArea.innerHTML = `<span class="spinner"></span><span>Step ${activeIndex + 1}/4: ${STEP_LABELS[activeIndex]}...</span>`;
}




document.getElementById("export-csv-btn").addEventListener("click", () => {
  downloadSettlementCSV(window.lastSettlementPlan);
});

document.getElementById("export-csv-btn").addEventListener("click", () => {
  downloadSettlementCSV(window.lastSettlementPlan);
});

document.getElementById("export-pdf-btn").addEventListener("click", async () => {
  const btn = document.getElementById("export-pdf-btn");
  const original = btn.textContent;
  btn.textContent = "Generating PDF...";
  btn.disabled = true;

  try {
    const response = await fetch("/api/export-pdf", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        settlement_plan: window.lastSettlementPlan || [],
        expense_summary: window.lastExpenseSummary || [],
      }),
    });

    if (!response.ok) {
      throw new Error("Export failed");
    }

    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "splitsense_settlement.pdf";
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert("Couldn't generate the PDF. Please try again.");
  } finally {
    btn.textContent = original;
    btn.disabled = false;
  }
});
renderPeople();
renderExpenseRows();
