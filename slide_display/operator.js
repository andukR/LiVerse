const waiting = document.querySelector("#waiting");
const candidateCard = document.querySelector("#candidate");
const ref = document.querySelector("#ref");
const verse = document.querySelector("#verse");
const asr = document.querySelector("#asr");
const connection = document.querySelector("#connection");
const status = document.querySelector("#status");
const approve = document.querySelector("#approve");
const reject = document.querySelector("#reject");
const manualRef = document.querySelector("#manualRef");
const manualApply = document.querySelector("#manualApply");
const manualStatus = document.querySelector("#manualStatus");
const bookNames = document.querySelector("#bookNames");
const bookSuggestions = document.querySelector("#bookSuggestions");
const bookGrid = document.querySelector("#bookGrid");
const chapterGrid = document.querySelector("#chapterGrid");
const verseGrid = document.querySelector("#verseGrid");
const pickedReference = document.querySelector("#pickedReference");
const bookStep = document.querySelector("#bookStep");
const chapterStep = document.querySelector("#chapterStep");
const verseStep = document.querySelector("#verseStep");
const rangeClear = document.querySelector("#rangeClear");
const rangeApply = document.querySelector("#rangeApply");
const pipelineStage = document.querySelector("#pipelineStage");
const pipelineProgress = document.querySelector("#pipelineProgress");
const pipelineMessage = document.querySelector("#pipelineMessage");
const manualHint = document.querySelector("#manualHint");
const manualCard = document.querySelector("#manualCard");
let books = [];
let bibleStructure = {};
const rangePick = {
  book: "",
  chapter: null,
  startVerse: null,
  endVerse: null,
};

const stageNames = {
  listening: "Слушаю",
  ner: "Распознаю речь",
  resolver: "Разбираю ссылку",
  direct: "Разбираю ссылку",
  quote_context: "Поиск по тексту стиха",
  not_found: "Ссылка не найдена",
  candidate: "Ожидает подтверждения",
  approved: "Отправлено",
  rejected: "Отклонено",
};

function renderProcessing(processing = {}) {
  const stage = processing.stage || "listening";
  const manualRequired = Boolean(processing.manual_required);
  pipelineStage.textContent = stageNames[stage] || "Распознавание";
  pipelineProgress.value = Number(processing.progress) || 0;
  pipelineMessage.textContent = processing.message || "LiVerse слушает речь";
  manualHint.classList.toggle("hidden", !manualRequired);
  manualCard.classList.toggle("attention", manualRequired);
}

function render(state) {
  renderProcessing((state && state.processing) || {});
  const candidate = state && state.candidate;
  waiting.classList.toggle("hidden", Boolean(candidate));
  candidateCard.classList.toggle("hidden", !candidate);
  if (!candidate) return;
  ref.textContent = candidate.ref || "Неизвестная ссылка";
  verse.textContent = candidate.verse || "";
  resizeCandidateVerse();
  asr.textContent = candidate.asr || candidate.detected_text || "";
  status.textContent = "";
}

function resizeCandidateVerse() {
  const length = verse.textContent.trim().length;
  verse.classList.toggle("long", length > 260);
  verse.classList.toggle("very-long", length > 520);
  verse.classList.toggle("extra-long", length > 900);
}

async function decide(action) {
  approve.disabled = true;
  reject.disabled = true;
  status.textContent = action === "approve" ? "Отправляю в Holyrics…" : "Отклоняю…";
  try {
    const response = await fetch(`/api/${action}`, { method: "POST" });
    const result = await response.json();
    if (!result.ok) throw new Error(result.reason || "Ошибка");
    status.textContent = action === "approve" ? "Отправлено" : "Отклонено";
  } catch (error) {
    status.textContent = `Ошибка: ${error.message}`;
  } finally {
    approve.disabled = false;
    reject.disabled = false;
  }
}

approve.addEventListener("click", () => decide("approve"));
reject.addEventListener("click", () => decide("reject"));

function bookQuery(value) {
  return value.trimStart().replace(/\s+\d.*$/, "").toLocaleLowerCase("ru-RU");
}

function renderBookSuggestions() {
  const query = bookQuery(manualRef.value);
  bookSuggestions.replaceChildren();
  if (!query || /\d/.test(manualRef.value)) return;
  books
    .filter((book) => book.toLocaleLowerCase("ru-RU").startsWith(query))
    .slice(0, 6)
    .forEach((book) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "book-suggestion";
      button.textContent = book;
      button.addEventListener("click", () => {
        manualRef.value = `${book} `;
        bookSuggestions.replaceChildren();
        manualRef.focus();
      });
      bookSuggestions.append(button);
    });
}

function referenceFromPick() {
  if (!rangePick.book || !rangePick.chapter || !rangePick.startVerse) return "";
  const chapter = rangePick.chapter;
  const start = rangePick.startVerse;
  const end = rangePick.endVerse;
  if (end && end !== start) {
    return `${rangePick.book} ${chapter}:${Math.min(start, end)}-${Math.max(start, end)}`;
  }
  return `${rangePick.book} ${chapter}:${start}`;
}

function setStep(activeStep) {
  bookStep.classList.toggle("active", activeStep === "book");
  chapterStep.classList.toggle("active", activeStep === "chapter");
  verseStep.classList.toggle("active", activeStep === "verse");
  bookGrid.classList.toggle("hidden", activeStep !== "book");
  chapterGrid.classList.toggle("hidden", activeStep !== "chapter");
  verseGrid.classList.toggle("hidden", activeStep !== "verse");
}

function updatePickedReference() {
  const ref = referenceFromPick();
  if (ref) {
    pickedReference.textContent = ref;
    manualRef.value = ref;
    return;
  }
  if (rangePick.book && rangePick.chapter) {
    pickedReference.textContent = `${rangePick.book} ${rangePick.chapter}: выберите стих или диапазон`;
    return;
  }
  if (rangePick.book) {
    pickedReference.textContent = `${rangePick.book}: выберите главу`;
    return;
  }
  pickedReference.textContent = "Выберите книгу";
}

function button(label, className, onClick) {
  const item = document.createElement("button");
  item.type = "button";
  item.className = className;
  item.textContent = label;
  item.addEventListener("click", onClick);
  return item;
}

function renderBooksGrid() {
  bookGrid.replaceChildren();
  books.forEach((book) => {
    const item = button(book, "picker-button book-button", () => {
      rangePick.book = book;
      rangePick.chapter = null;
      rangePick.startVerse = null;
      rangePick.endVerse = null;
      renderChaptersGrid();
      updatePickedReference();
      setStep("chapter");
    });
    item.classList.toggle("selected", rangePick.book === book);
    bookGrid.append(item);
  });
}

function renderChaptersGrid() {
  chapterGrid.replaceChildren();
  const chapters = bibleStructure[rangePick.book] || {};
  Object.keys(chapters)
    .map(Number)
    .sort((a, b) => a - b)
    .forEach((chapter) => {
      const item = button(String(chapter), "picker-button number-button", () => {
        rangePick.chapter = chapter;
        rangePick.startVerse = null;
        rangePick.endVerse = null;
        renderChaptersGrid();
        renderVersesGrid();
        updatePickedReference();
        setStep("verse");
      });
      item.classList.toggle("selected", rangePick.chapter === chapter);
      chapterGrid.append(item);
    });
}

function verseClass(verse) {
  const start = rangePick.startVerse;
  const end = rangePick.endVerse;
  if (verse === start || verse === end) return "selected";
  if (start && end && verse > Math.min(start, end) && verse < Math.max(start, end)) {
    return "in-range";
  }
  return "";
}

function chooseVerse(verse) {
  if (!rangePick.startVerse || (rangePick.startVerse && rangePick.endVerse)) {
    rangePick.startVerse = verse;
    rangePick.endVerse = null;
  } else if (verse === rangePick.startVerse) {
    rangePick.startVerse = null;
    rangePick.endVerse = null;
  } else {
    rangePick.endVerse = verse;
  }
  renderVersesGrid();
  updatePickedReference();
}

function renderVersesGrid() {
  verseGrid.replaceChildren();
  const chapters = bibleStructure[rangePick.book] || {};
  const verses = chapters[String(rangePick.chapter)] || [];
  verses.forEach((verse) => {
    const item = button(String(verse), "picker-button number-button", () => chooseVerse(Number(verse)));
    const state = verseClass(Number(verse));
    if (state) item.classList.add(state);
    verseGrid.append(item);
  });
}

function resetRangePick() {
  rangePick.book = "";
  rangePick.chapter = null;
  rangePick.startVerse = null;
  rangePick.endVerse = null;
  manualRef.value = "";
  renderBooksGrid();
  chapterGrid.replaceChildren();
  verseGrid.replaceChildren();
  updatePickedReference();
  setStep("book");
}

function applyRangePick() {
  const value = referenceFromPick();
  if (!value) {
    manualStatus.textContent = "Выберите книгу, главу и стих";
    return;
  }
  manualRef.value = value;
  applyManualReference();
}

async function loadBooks() {
  try {
    const response = await fetch("/api/bible-structure");
    const result = await response.json();
    books = result.books || [];
    bibleStructure = result.structure || {};
    books.forEach((book) => {
      const option = document.createElement("option");
      option.value = book;
      bookNames.append(option);
    });
    renderBooksGrid();
    setStep("book");
  } catch {
    manualStatus.textContent = "Не удалось загрузить список книг";
  }
}

async function applyManualReference() {
  const value = manualRef.value.trim();
  if (!value) {
    manualStatus.textContent = "Введите книгу, главу и стих";
    return;
  }
  manualApply.disabled = true;
  manualStatus.textContent = "Проверяю ссылку…";
  try {
    const response = await fetch("/api/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref: value }),
    });
    const result = await response.json();
    if (!result.ok) throw new Error("книга, глава или стих не найдены");
    manualRef.value = result.candidate.ref;
    manualStatus.textContent = "Цитата подставлена. Проверьте и нажмите «Принять».";
  } catch (error) {
    manualStatus.textContent = `Ошибка: ${error.message}`;
  } finally {
    manualApply.disabled = false;
  }
}

manualRef.addEventListener("input", renderBookSuggestions);
manualRef.addEventListener("keydown", (event) => {
  if (event.key === "Enter") applyManualReference();
});
manualApply.addEventListener("click", applyManualReference);
rangeClear.addEventListener("click", resetRangePick);
rangeApply.addEventListener("click", applyRangePick);
bookStep.addEventListener("click", () => setStep("book"));
chapterStep.addEventListener("click", () => {
  if (rangePick.book) setStep("chapter");
});
verseStep.addEventListener("click", () => {
  if (rangePick.book && rangePick.chapter) setStep("verse");
});

const events = new EventSource("/operator-events");
events.onopen = () => { connection.textContent = "Телефон подключён"; };
events.onmessage = (event) => {
  try { render(JSON.parse(event.data)); }
  catch { connection.textContent = "Ошибка данных"; }
};
events.onerror = () => { connection.textContent = "Переподключение…"; };
loadBooks();
