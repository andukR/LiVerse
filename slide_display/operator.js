const waiting = document.querySelector("#waiting");
const candidateCard = document.querySelector("#candidate");
const ref = document.querySelector("#ref");
const verse = document.querySelector("#verse");
const asr = document.querySelector("#asr");
const connection = document.querySelector("#connection");
const status = document.querySelector("#status");
const approve = document.querySelector("#approve");
const approveContext = document.querySelector("#approveContext");
const reject = document.querySelector("#reject");
const manualRef = document.querySelector("#manualRef");
const manualStatus = document.querySelector("#manualStatus");
const bookNames = document.querySelector("#bookNames");
const bookSuggestions = document.querySelector("#bookSuggestions");
const bookTopicTabs = document.querySelector("#bookTopicTabs");
const bookGrid = document.querySelector("#bookGrid");
const chapterGrid = document.querySelector("#chapterGrid");
const verseGrid = document.querySelector("#verseGrid");
const pickedReference = document.querySelector("#pickedReference");
const bookStep = document.querySelector("#bookStep");
const chapterStep = document.querySelector("#chapterStep");
const verseStep = document.querySelector("#verseStep");
const pipelineStage = document.querySelector("#pipelineStage");
const pipelineProgress = document.querySelector("#pipelineProgress");
const pipelineMessage = document.querySelector("#pipelineMessage");
const manualHint = document.querySelector("#manualHint");
const manualCard = document.querySelector("#manualCard");
const sessionQuoteCount = document.querySelector("#sessionQuoteCount");
const whatsappShare = document.querySelector("#whatsappShare");
const copySessionQuotes = document.querySelector("#copySessionQuotes");
let books = [];
let bibleStructure = {};
let applyPickTimer = null;
let activeBookTopic = "old";
let multiTapState = { key: "", count: 0, timer: null };
let sessionShareText = "";
const rangePick = {
  book: "",
  chapter: null,
  startVerse: null,
  endVerse: null,
};

const bookTopics = [
  {
    key: "old",
    label: "Ветхий Завет",
    books: [
      ["Быт", ["Бытие"]],
      ["Исх", ["Исход"]],
      ["Лев", ["Левит"]],
      ["Чис", ["Числа"]],
      ["Втор", ["Второзаконие"]],
      ["Ис.Нав", ["Иисус Навин"]],
      ["Суд", ["Судьи"]],
      ["Руф", ["Руфь"]],
      ["Цар", ["1 Царств", "2 Царств", "3 Царств", "4 Царств"]],
      ["Пар", ["1 Паралипоменон", "2 Паралипоменон"]],
      ["Ездр", ["Ездра"]],
      ["Неем", ["Неемия"]],
      ["Есф", ["Есфирь"]],
      ["Иов", ["Иов"]],
      ["Пс", ["Псалтирь"]],
      ["Притч", ["Притчи"]],
      ["Еккл", ["Екклесиаст"]],
      ["Песн", ["Песня Песней"]],
    ],
  },
  {
    key: "prophets",
    label: "Пророки",
    books: [
      ["Ис", ["Исаия"]],
      ["Иер", ["Иеремия"]],
      ["Плач", ["Плач Иеремии"]],
      ["Иез", ["Иезекииль"]],
      ["Дан", ["Даниил"]],
      ["Ос", ["Осия"]],
      ["Иоил", ["Иоиль"]],
      ["Ам", ["Амос"]],
      ["Авд", ["Авдий"]],
      ["Ион", ["Иона"]],
      ["Мих", ["Михей"]],
      ["Наум", ["Наум"]],
      ["Авв", ["Аввакум"]],
      ["Соф", ["Софония"]],
      ["Агг", ["Аггей"]],
      ["Зах", ["Захария"]],
      ["Мал", ["Малахия"]],
      ["Откр", ["Откровение"], "aside"],
    ],
  },
  {
    key: "new",
    label: "Новый Завет",
    books: [
      ["Матф", ["Матфей"]],
      ["Марк", ["Марк"]],
      ["Лук", ["Лука"]],
      ["Иоан", ["Иоанн"]],
      ["Деян", ["Деяния"]],
      ["Иак", ["Иаков"]],
      ["Петр", ["1 Петра", "2 Петра"]],
      ["Иоан", ["1 Иоанна", "2 Иоанна", "3 Иоанна"]],
      ["Иуд", ["Иуда"]],
      ["Рим", ["Римлянам"]],
      ["Кор", ["1 Коринфянам", "2 Коринфянам"]],
      ["Гал", ["Галатам"]],
      ["Еф", ["Ефесянам"]],
      ["Фил", ["Филиппийцам"]],
      ["Кол", ["Колоссянам"]],
      ["Фесс", ["1 Фессалоникийцам", "2 Фессалоникийцам"]],
      ["Тим", ["1 Тимофею", "2 Тимофею"]],
      ["Тит", ["Титу"]],
      ["Флм", ["Филимону"]],
      ["Евр", ["Евреям"]],
    ],
  },
];

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
  renderSessionShare((state && state.session_share) || {});
  const candidate = state && state.candidate;
  waiting.classList.toggle("hidden", Boolean(candidate));
  candidateCard.classList.toggle("hidden", !candidate);
  if (!candidate) return;
  ref.textContent = candidate.ref || "Неизвестная ссылка";
  verse.textContent = candidate.verse || "";
  resizeCandidateVerse();
  asr.textContent = candidate.asr || candidate.detected_text || "";
  approveContext.classList.toggle("hidden", !candidate.can_set_context);
  status.textContent = "";
}

function renderSessionShare(share = {}) {
  const count = Number(share.count) || 0;
  sessionShareText = share.text || "";
  sessionQuoteCount.textContent = `Цитат: ${count}`;
  whatsappShare.href = share.whatsapp_url || "#";
  whatsappShare.classList.toggle("disabled", !share.whatsapp_url);
  copySessionQuotes.disabled = !sessionShareText;
}

function resizeCandidateVerse() {
  const length = verse.textContent.trim().length;
  verse.classList.toggle("long", length > 260);
  verse.classList.toggle("very-long", length > 520);
  verse.classList.toggle("extra-long", length > 900);
}

async function decide(action) {
  approve.disabled = true;
  approveContext.disabled = true;
  reject.disabled = true;
  status.textContent = (
    action === "approve-context"
      ? "Отправляю и запоминаю контекст…"
      : (action === "approve" ? "Отправляю в Holyrics…" : "Отклоняю…")
  );
  try {
    const response = await fetch(`/api/${action}`, { method: "POST" });
    const result = await response.json();
    if (!result.ok) throw new Error(result.reason || "Ошибка");
    status.textContent = (
      action === "approve-context"
        ? "Контекст запомнен"
        : (action === "approve" ? "Отправлено" : "Отклонено")
    );
  } catch (error) {
    status.textContent = `Ошибка: ${error.message}`;
  } finally {
    approve.disabled = false;
    approveContext.disabled = false;
    reject.disabled = false;
  }
}

approve.addEventListener("click", () => decide("approve"));
approveContext.addEventListener("click", () => decide("approve-context"));
reject.addEventListener("click", () => decide("reject"));
copySessionQuotes.addEventListener("click", async () => {
  if (!sessionShareText) return;
  try {
    await navigator.clipboard.writeText(sessionShareText);
    manualStatus.textContent = "Список цитат скопирован";
  } catch {
    manualStatus.textContent = sessionShareText;
  }
});

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

function selectBook(book) {
  rangePick.book = book;
  rangePick.chapter = null;
  rangePick.startVerse = null;
  rangePick.endVerse = null;
  renderBooksGrid();
  renderChaptersGrid();
  updatePickedReference();
  setStep("chapter");
}

function renderBookTopicTabs() {
  bookTopicTabs.replaceChildren();
  bookTopics.forEach((topic) => {
    const item = button(topic.label, "book-topic-button", () => {
      activeBookTopic = topic.key;
      window.clearTimeout(multiTapState.timer);
      multiTapState = { key: "", count: 0, timer: null };
      renderBookTopicTabs();
      renderBooksGrid();
      setStep("book");
    });
    item.classList.toggle("active", activeBookTopic === topic.key);
    bookTopicTabs.append(item);
  });
}

function visibleBookItems() {
  const topic = bookTopics.find((item) => item.key === activeBookTopic) || bookTopics[0];
  return topic.books.filter(([_label, variants]) => variants.some((book) => bibleStructure[book]));
}

function bookButtonTitle(label, variants) {
  if (variants.length <= 1) return variants[0];
  return `${label}: ${variants.map((book, index) => `${index + 1} - ${book}`).join(", ")}`;
}

function scheduleMultiTapBook(key, label, variants) {
  window.clearTimeout(multiTapState.timer);
  if (multiTapState.key === key) {
    multiTapState.count += 1;
  } else {
    multiTapState = { key, count: 1, timer: null };
  }
  const count = Math.min(multiTapState.count, variants.length);
  pickedReference.textContent = `${label}: ${count} - ${variants[count - 1]}`;
  multiTapState.timer = window.setTimeout(() => {
    selectBook(variants[count - 1]);
    multiTapState = { key: "", count: 0, timer: null };
  }, 360);
}

function renderBooksGrid() {
  bookGrid.replaceChildren();
  visibleBookItems().forEach(([label, variants, marker], index) => {
    const key = `${activeBookTopic}:${label}:${index}`;
    const item = button(label, "picker-button book-button short-book-button", () => {
      if (variants.length > 1) {
        scheduleMultiTapBook(key, label, variants);
        return;
      }
      selectBook(variants[0]);
    });
    item.title = bookButtonTitle(label, variants);
    item.dataset.fullName = variants.length > 1 ? `${variants.length} кн.` : variants[0];
    if (marker === "aside") item.classList.add("aside-book");
    if (variants.includes(rangePick.book)) item.classList.add("selected");
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
  scheduleApplyPickedReference();
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

function scheduleApplyPickedReference() {
  window.clearTimeout(applyPickTimer);
  applyPickTimer = window.setTimeout(applyPickedReference, 250);
}

function applyPickedReference() {
  const value = referenceFromPick();
  if (!value) {
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
    renderBookTopicTabs();
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
  }
}

manualRef.addEventListener("input", renderBookSuggestions);
manualRef.addEventListener("keydown", (event) => {
  if (event.key === "Enter") applyManualReference();
});
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
