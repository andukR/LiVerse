const audio = document.querySelector("#audio");
const refText = document.querySelector("#refText");
const verseText = document.querySelector("#verseText");
const transitionBanner = document.querySelector("#transitionBanner");
const progressText = document.querySelector("#progressText");
const decisionText = document.querySelector("#decisionText");
const decisionReason = document.querySelector("#decisionReason");
const speechText = document.querySelector("#speechText");
const note = document.querySelector("#note");
const statusText = document.querySelector("#statusText");
const timeText = document.querySelector("#timeText");
const audioProgress = document.querySelector("#audioProgress");
const startReplay = document.querySelector("#startReplay");
const continueReplay = document.querySelector("#continueReplay");
const rewindReplay = document.querySelector("#rewindReplay");
const restartReplay = document.querySelector("#restartReplay");
const stopError = document.querySelector("#stopError");
const errorPanel = document.querySelector("#errorPanel");
const incidentReport = document.querySelector("#incidentReport");
const copyIncident = document.querySelector("#copyIncident");
let timeline = null;
let trackIndex = 0;
let decisionIndex = 0;
let displayIndex = 0;
let currentError = null;
let currentIncidentId = null;
let bannerTimer = null;
let playbackFrame = null;
const observedEventIds = new Set();

function slideReference(element, passage) {
  const passageMatch = String(passage || "").match(/^(.*?)\s+(\d+):(\d+)/);
  const book = passageMatch?.[1] || String(passage || "");
  const startChapter = Number(element?.start_chapter ?? element?.chapter ?? passageMatch?.[2] ?? 0);
  const startVerse = Number(element?.start_verse ?? element?.verse ?? 0);
  const endChapter = Number(element?.chapter ?? startChapter);
  const endVerse = Number(element?.verse ?? startVerse);
  if (!startChapter || !startVerse) return passage || " ";
  let bounds = `${startChapter}:${startVerse}`;
  if (endChapter !== startChapter || endVerse !== startVerse) {
    bounds += endChapter !== startChapter ? `–${endChapter}:${endVerse}` : `–${endVerse}`;
  }
  return `${book} ${bounds}`.trim();
}

function showSlide(element, passage, emptyText = "Текст слайда отсутствует в этом старом журнале.") {
  refText.textContent = slideReference(element, passage);
  verseText.textContent = element?.text ?? emptyText;
  verseText.classList.toggle("long", verseText.textContent.length > 230);
  verseText.classList.toggle("very-long", verseText.textContent.length > 520);
}

function currentTrack() {
  return timeline?.tracks?.[trackIndex] || null;
}

function showBanner(text, hold = false) {
  clearTimeout(bannerTimer);
  transitionBanner.textContent = text;
  transitionBanner.className = hold ? "transition-banner hold show" : "transition-banner show";
  transitionBanner.hidden = false;
  bannerTimer = setTimeout(() => { transitionBanner.hidden = true; }, 1600);
}

function applyDecision(decision) {
  observedEventIds.add(decision.event_id);
  if (decision.action === "activate" && decision.current?.text) {
    showSlide(decision.current, timeline.passage);
    showBanner(`УПС: ${decision.action_label}`);
  } else if (decision.will_transition && decision.target?.text) {
    showSlide(decision.target, timeline.passage);
    showBanner(`УПС: ${decision.action_label}`);
  } else {
    showBanner("УПС: оставить текущий слайд", true);
  }
  decisionText.textContent = `Последнее решение: ${decision.action_label || decision.action}`;
  decisionReason.textContent = `Основание: ${decision.reason || "не указано"}; оценка ${decision.score ?? "—"}, запас ${decision.margin ?? "—"}.`;
  speechText.textContent = decision.window || "Распознанный фрагмент отсутствует.";
}

function applyDisplayUpdate(update) {
  const announcement = update.kind === "range_announcement";
  const sequentialReading = update.kind === "sequential_reading";
  const readingList = update.kind === "reference_list";
  showSlide(update.element || {}, update.ref || timeline.passage, announcement ? "" : undefined);
  showBanner(announcement ? `LiVerse: объявлен диапазон ${update.ref}` : sequentialReading ? "УПС: обнаружено последовательное чтение" : readingList ? "LiVerse: список ссылок дополнен" : `LiVerse: новая ссылка ${update.ref}`);
  decisionText.textContent = announcement ? `Объявлен диапазон: ${update.ref}` : sequentialReading ? `УПС начал последовательный показ: ${slideReference(update.element, update.ref)}` : readingList ? "LiVerse дополнил список ссылок" : `LiVerse показал: ${update.ref}`;
  decisionReason.textContent = announcement
    ? "Текст диапазона не выводится целиком: дальше слайдами управляет УПС."
    : sequentialReading
      ? "Текст подтвердил непрерывное чтение. Показан текущий подтверждённый стих; далее УПС будет переходить по одному стиху."
    : readingList
      ? "Ссылки добавлены к уже показанному списку."
    : "Ссылка распознана обычным механизмом LiVerse.";
  if (update.vosk_text) speechText.textContent = update.vosk_text;
}

function showNeutralSlide() {
  refText.textContent = "Эмуляция ещё не дошла до ссылки";
  verseText.textContent = "—";
  verseText.classList.remove("long", "very-long");
}

function resetTrack(index) {
  trackIndex = index;
  decisionIndex = 0;
  displayIndex = 0;
  const track = currentTrack();
  if (!track) return;
  showNeutralSlide();
  audio.src = track.audio_available ? `/api/audio?event=${encodeURIComponent(track.audio_event_id)}` : "";
  audioProgress.value = 0;
  statusText.textContent = track.audio_available ? "Готово к непрерывной эмуляции." : "WAV-файл этого участка не найден.";
  startReplay.disabled = !track.audio_available;
  rewindReplay.disabled = !track.audio_available;
  restartReplay.disabled = !track.audio_available;
  stopError.disabled = true;
}

function processUntil(seconds) {
  const track = currentTrack();
  if (!track) return;
  const displays = track.display_updates || [];
  while (true) {
    const decision = track.decisions[decisionIndex];
    const display = displays[displayIndex];
    const decisionTime = decision ? Number(decision.replay_seconds || 0) : Infinity;
    const displayTime = display ? Number(display.replay_seconds || 0) : Infinity;
    if (Math.min(decisionTime, displayTime) > seconds) break;
    if (decisionTime <= displayTime) {
      applyDecision(decision);
      decisionIndex += 1;
    } else {
      applyDisplayUpdate(display);
      displayIndex += 1;
    }
  }
}

function syncPlayback() {
  const track = currentTrack();
  if (!track) return;
  audioProgress.value = audio.currentTime;
  timeText.textContent = `${Math.floor(audio.currentTime / 60)}:${String(Math.floor(audio.currentTime % 60)).padStart(2, "0")} / ${Math.floor((audio.duration || 0) / 60)}:${String(Math.floor((audio.duration || 0) % 60)).padStart(2, "0")}`;
  processUntil(audio.currentTime);
}

function stopPlaybackClock() {
  if (playbackFrame !== null) cancelAnimationFrame(playbackFrame);
  playbackFrame = null;
}

function startPlaybackClock() {
  stopPlaybackClock();
  const tick = () => {
    syncPlayback();
    if (!audio.paused && !audio.ended) playbackFrame = requestAnimationFrame(tick);
  };
  playbackFrame = requestAnimationFrame(tick);
}

function rebuildAt(seconds) {
  const track = currentTrack();
  if (!track) return;
  decisionIndex = 0;
  displayIndex = 0;
  showNeutralSlide();
  processUntil(seconds);
}

function setTimeline(nextTimeline) {
  timeline = nextTimeline;
  observedEventIds.clear();
  currentError = null;
  currentIncidentId = null;
  errorPanel.hidden = true;
  incidentReport.value = "";
  continueReplay.hidden = true;
  startReplay.hidden = false;
  progressText.textContent = `Диапазон ${timeline.sequence_number}/${timeline.sequence_total}: ${timeline.passage}; неразмечено: ${timeline.remaining}`;
  decisionText.textContent = "УПС ещё не принял решение";
  decisionReason.textContent = "Во время правильной работы ничего нажимать не нужно.";
  speechText.textContent = "—";
  resetTrack(0);
}

async function request(path, payload) {
  const response = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok || !data.ok) throw new Error(data.error || "Операция не выполнена.");
  return data;
}

async function loadTimeline() {
  const response = await fetch("/api/timeline", { cache: "no-store" });
  if (!response.ok) throw new Error("Не удалось получить временную шкалу УПС.");
  setTimeline(await response.json());
}

async function playCurrentTrack() {
  startReplay.hidden = true;
  continueReplay.hidden = true;
  errorPanel.hidden = true;
  stopError.disabled = false;
  try {
    await audio.play();
    startPlaybackClock();
    statusText.textContent = "Эмуляция идёт. При ошибке нажмите красную кнопку или клавишу Enter.";
  } catch {
    startReplay.hidden = false;
    stopError.disabled = true;
    statusText.textContent = "Браузер запретил запуск звука. Нажмите «Запустить эмуляцию» ещё раз.";
  }
}

async function finishSequence() {
  stopError.disabled = true;
  statusText.textContent = "Сохраняю просмотренные правильные решения…";
  const result = await request("/api/complete", { observed_event_ids: [...observedEventIds] });
  if (result.finished) {
    progressText.textContent = "Все доступные последовательности просмотрены";
    statusText.textContent = "Готово. Непросмотренные решения не были размечены автоматически.";
    startReplay.hidden = true;
    return;
  }
  setTimeline(result.timeline);
  await playCurrentTrack();
}

audio.addEventListener("loadedmetadata", () => { audioProgress.max = audio.duration || 1; });
audio.addEventListener("timeupdate", () => {
  syncPlayback();
});
audio.addEventListener("ended", async () => {
  stopPlaybackClock();
  try {
    if (trackIndex + 1 < timeline.tracks.length) {
      resetTrack(trackIndex + 1);
      await playCurrentTrack();
    } else {
      await finishSequence();
    }
  } catch (error) { statusText.textContent = error.message; }
});

startReplay.addEventListener("click", playCurrentTrack);
continueReplay.addEventListener("click", playCurrentTrack);
rewindReplay.addEventListener("click", async () => {
  if (!currentTrack()?.audio_available) return;
  audio.pause();
  stopPlaybackClock();
  const target = Math.max(0, audio.currentTime - 10);
  audio.currentTime = target;
  rebuildAt(target);
  statusText.textContent = `Возврат на ${target.toFixed(1)} с. Воспроизведение продолжается.`;
  await playCurrentTrack();
});
restartReplay.addEventListener("click", async () => {
  if (!currentTrack()?.audio_available) return;
  audio.pause();
  stopPlaybackClock();
  audio.currentTime = 0;
  rebuildAt(0);
  statusText.textContent = "Возврат к началу фрагмента. Эмуляция запускается заново.";
  await playCurrentTrack();
});
stopError.addEventListener("click", async () => {
  const track = currentTrack();
  if (!track || !track.decisions.length) return;
  audio.pause();
  stopPlaybackClock();
  stopError.disabled = true;
  const hasObservedDecision = decisionIndex > 0;
  currentError = hasObservedDecision ? track.decisions[decisionIndex - 1] : null;
  const anchor = currentError || track.decisions[0];
  try {
    const incident = await request("/api/incident", {
      event_id: anchor.event_id,
      operator_stop_seconds: audio.currentTime,
      displayed_ref: refText.textContent,
      note: note.value.trim(),
    });
    currentIncidentId = incident.incident_id || null;
    incidentReport.value = incident.report || "";
    errorPanel.hidden = false;
    continueReplay.hidden = false;
    statusText.textContent = "Наблюдение сохранено отдельно. При уверенности выберите тип ошибки; иначе запишите заметку и повторите фрагмент.";
  } catch (error) { statusText.textContent = error.message; }
});

copyIncident.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(incidentReport.value);
    statusText.textContent = "Диагностический отчёт скопирован.";
  } catch {
    incidentReport.focus();
    incidentReport.select();
    statusText.textContent = "Нажмите Ctrl+C, чтобы скопировать выделенный отчёт.";
  }
});

document.querySelectorAll("[data-error]").forEach((button) => button.addEventListener("click", async () => {
  if (!currentError) return;
  try {
    await request("/api/error", { event_id: currentError.event_id, category: button.dataset.error, note: note.value.trim() });
    currentError.review_category = button.dataset.error;
    statusText.textContent = "Тип ошибки уточнён и сохранён.";
  } catch (error) { statusText.textContent = error.message; }
}));
document.querySelector("#saveErrorNote").addEventListener("click", async () => {
  if (!currentIncidentId) return;
  try {
    await request("/api/incident-note", { incident_id: currentIncidentId, note: note.value.trim() });
    statusText.textContent = "Наблюдение сохранено без метки для обучения.";
  } catch (error) { statusText.textContent = error.message; }
});
document.addEventListener("keydown", (event) => {
  if (event.target instanceof HTMLTextAreaElement) return;
  if (event.key === "Enter" && !stopError.disabled) stopError.click();
  if (event.key === " " && !startReplay.hidden) { event.preventDefault(); startReplay.click(); }
});

loadTimeline().catch((error) => { statusText.textContent = error.message; });
