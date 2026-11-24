"use strict";

const byId = (id) => document.getElementById(id);
const characterKey = "dice.character";
const messageField = byId("message");
const timeline = byId("timeline");
const messageStatus = byId("message-status");
const profileDialog = byId("profile-dialog");
const favoriteDialog = byId("favorite-dialog");
const adminDialog = byId("admin-dialog");
const adminStatus = byId("admin-status");
const targetField = byId("telegram-target");
const descriptionField = byId("favorite-description");
const timeFormatter = new Intl.DateTimeFormat("ru", {
  day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
});

function readLocal(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
}

const savedCharacter = readLocal(characterKey, {});
const character = {
  id: typeof savedCharacter.id === "string" ? savedCharacter.id : crypto.randomUUID(),
  name: typeof savedCharacter.name === "string" ? savedCharacter.name : "",
};
const favoritesKey = "dice.favorites." + character.id;
const saved = readLocal(favoritesKey, []);
let favorites = Array.isArray(saved)
  ? saved.filter((favorite) => favorite && typeof (favorite.text ?? favorite.formula) === "string")
    .map((favorite) => ({ ...favorite, text: favorite.text ?? favorite.formula }))
  : [];
let favoriteDraft;
let signedIn = false;
let activeTarget;
let draftTarget;
let feedRequest = 0;
let pendingFeed = null;
let renderedFeed = "";
let messages = [];

function setStatus(element, message, error = false) {
  element.textContent = message;
  element.classList.toggle("error", error);
}

function storeLocal(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    setStatus(messageStatus, "Этот браузер не сохранил персонажа или избранное.", true);
  }
}

async function api(path, method = "GET", data) {
  const response = await fetch(path, {
    method,
    cache: "no-store",
    ...(data === undefined ? {} : {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    }),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(result.error || (response.status === 429
      ? "Слишком много запросов. Попробуйте чуть позже."
      : "Сайт временно недоступен. Попробуйте ещё раз."));
    error.status = response.status;
    throw error;
  }
  return result;
}

function element(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}

function initials(name) {
  return name.trim().split(/\s+/).slice(0, 2).map((word) => Array.from(word)[0]).join("").toUpperCase() || "?";
}

function updateProfile() {
  byId("profile-avatar").textContent = initials(character.name);
  byId("profile-name").textContent = character.name || "Имя персонажа";
}

function openProfile() {
  byId("character-name").value = character.name;
  profileDialog.showModal();
  byId("character-name").focus();
}

function toggleDrawer(open, returnFocus = false) {
  if (!document.body.classList.contains("show-saved") && !open) return;
  document.body.classList.toggle("show-saved", open);
  byId("open-saved").setAttribute("aria-expanded", String(open));
  if (open) byId("close-saved").focus();
  else if (returnFocus) byId("open-saved").focus();
}

function showTarget(target) {
  const changed = activeTarget !== target;
  activeTarget = target;
  byId("connection").textContent = target === null ? "Веб-чат"
    : target.startsWith("-") ? "Группа Telegram" : "Личный чат Telegram";
  if (changed && draftTarget !== undefined && draftTarget !== target && messageField.value.trim()) {
    setStatus(messageStatus, "Чат сменился. Измените черновик, чтобы отправить его в новый чат.", true);
  }
  return changed;
}

function showSession(authenticated, session = {}) {
  signedIn = authenticated;
  feedRequest++;
  pendingFeed = null;
  byId("boot-status").hidden = true;
  byId("login-view").hidden = authenticated;
  byId("table-view").hidden = !authenticated;
  byId("top-actions").hidden = !authenticated;
  byId("admin-button").hidden = !authenticated || !session.admin_enabled;
  if (authenticated) {
    showTarget(session.target_id);
    storeLocal(characterKey, character);
    updateProfile();
    renderFavorites();
    refreshFeed(true);
  } else {
    if (favoriteDialog.open) favoriteDialog.close();
    if (profileDialog.open) profileDialog.close();
    if (adminDialog.open) adminDialog.close();
    toggleDrawer(false);
    byId("feed").replaceChildren();
    byId("chat-empty").hidden = false;
    messages = [];
    renderedFeed = "";
    setStatus(byId("feed-status"), "");
    setStatus(messageStatus, "");
  }
}

function handleError(error, target) {
  if (error.status === 401 && signedIn) {
    showSession(false);
    setStatus(byId("login-status"), "Сессия закончилась. Войдите снова.", true);
  } else {
    setStatus(target, error.message, true);
  }
}

function renderFavorites() {
  const items = favorites.map((favorite) => {
    const row = element("li", undefined, "saved-item");
    const use = element("button", undefined, "favorite-use");
    use.type = "button";
    use.append(element("span", favorite.text.length > 160 ? favorite.text.slice(0, 160) + "…" : favorite.text));
    if (favorite.description) use.append(element("span", favorite.description, "favorite-description"));
    use.addEventListener("click", () => {
      messageField.value = favorite.text;
      draftTarget = activeTarget;
      toggleDrawer(false);
      messageField.focus();
    });

    const edit = element("button", "✎", "favorite-control");
    edit.type = "button";
    edit.setAttribute("aria-label", "Изменить описание");
    edit.title = "Изменить описание";
    edit.addEventListener("click", () => {
      favoriteDraft = favorite;
      byId("favorite-text").textContent = favorite.text;
      descriptionField.value = favorite.description || "";
      favoriteDialog.showModal();
      descriptionField.focus();
    });

    const remove = element("button", "×", "favorite-control");
    remove.type = "button";
    remove.setAttribute("aria-label", "Удалить из избранного");
    remove.title = "Удалить из избранного";
    remove.addEventListener("click", () => {
      favorites = favorites.filter((candidate) => candidate.id !== favorite.id);
      saveFavorites();
    });
    row.append(use, edit, remove);
    return row;
  });
  byId("saved-empty").hidden = items.length !== 0;
  byId("favorites").replaceChildren(...items);
}

function saveFavorites() {
  storeLocal(favoritesKey, favorites);
  renderFavorites();
  renderFeed(false);
}

function matchingFavorite(message) {
  return favorites.find((favorite) => favorite.entryId === message.id
    || (!message.text_truncated && favorite.text === message.text));
}

async function toggleFavorite(message, button) {
  button.disabled = true;
  try {
    const selected = matchingFavorite(message);
    if (selected) {
      favorites = favorites.filter((favorite) => favorite.id !== selected.id);
    } else {
      const { text } = message.text_truncated ? await api("/api/chat/" + message.id + "/text") : message;
      favorites.push({ id: crypto.randomUUID(), entryId: message.id, text, description: "" });
    }
    saveFavorites();
  } catch (error) {
    handleError(error, messageStatus);
  } finally {
    button.disabled = false;
  }
}

function appendDetails(target, html) {
  const body = new DOMParser().parseFromString(html, "text/html").body;
  for (const node of body.childNodes) {
    if (node.nodeName === "S" || node.nodeName === "B") {
      target.append(element(node.nodeName.toLowerCase(), node.textContent));
    } else {
      target.append(document.createTextNode(node.textContent));
    }
  }
}

const deliveryText = {
  queued: "В очереди в Telegram",
  sending: "Отправляется в Telegram",
  failed: "Telegram отклонил отправку",
  uncertain: "Доставка в Telegram не подтверждена",
};

function renderMessage(message) {
  const own = message.character_id === character.id;
  const item = element("li", undefined, "message" + (own ? " is-own" : ""));
  item.dataset.messageId = String(message.id);
  const avatar = element("span", initials(message.name), "avatar");
  avatar.setAttribute("aria-hidden", "true");
  const body = element("div", undefined, "message-body");
  const meta = element("div", undefined, "message-meta");
  const timestamp = element("time", timeFormatter.format(new Date(message.created_at)));
  timestamp.dateTime = message.created_at;
  meta.append(element("strong", message.name), timestamp);
  if (message.source === "telegram") meta.append(element("span", "Telegram"));
  body.append(meta, element("p", message.text + (message.text_truncated ? "…" : ""), "message-text"));
  if (message.total !== null) {
    const result = element("div", undefined, "message-results");
    if (message.result_html) {
      const detail = element("p", undefined, "roll-details");
      appendDetails(detail, message.result_html);
      result.append(detail);
    } else {
      result.append(element("strong", message.total, "roll-total"));
    }
    if (message.has_download) {
      const link = element("a", "Скачать полный бросок", "roll-download");
      link.href = "/api/chat/" + message.id + "/result";
      result.append(link);
    }
    body.append(result);
  }

  const footer = element("div", undefined, "message-footer");
  if (Object.hasOwn(deliveryText, message.delivery)) footer.append(element("span", deliveryText[message.delivery]));
  const isSaved = Boolean(matchingFavorite(message));
  const label = isSaved ? "Убрать из избранного" : "Сохранить в избранное";
  const save = element("button", isSaved ? "♥" : "♡", "save-toggle" + (isSaved ? " is-saved" : ""));
  save.type = "button";
  save.setAttribute("aria-label", label);
  save.setAttribute("aria-pressed", String(isSaved));
  save.title = label;
  save.addEventListener("click", () => toggleFavorite(message, save));
  footer.append(save);
  body.append(footer);
  item.append(avatar, body);
  return item;
}

function renderFeed(force) {
  const scrollTop = timeline.scrollTop;
  const nearEnd = timeline.scrollHeight - timeline.clientHeight - scrollTop < 100;
  byId("chat-empty").hidden = messages.length !== 0;
  byId("feed").replaceChildren(...messages.map(renderMessage));
  timeline.scrollTop = force || nearEnd ? timeline.scrollHeight : scrollTop;
}

async function refreshFeed(force = false) {
  if (!signedIn || (pendingFeed !== null && !force)) return;
  const refresh = ++feedRequest;
  pendingFeed = refresh;
  try {
    const data = await api("/api/chat");
    if (refresh !== feedRequest) return;
    const changed = showTarget(data.target_id);
    const latest = JSON.stringify(data.messages);
    if (changed || latest !== renderedFeed) {
      messages = data.messages;
      renderedFeed = latest;
      renderFeed(force || changed);
    }
    setStatus(byId("feed-status"), "");
  } catch (error) {
    if (refresh === feedRequest) handleError(error, byId("feed-status"));
  } finally {
    if (pendingFeed === refresh) pendingFeed = null;
  }
}

byId("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  setStatus(byId("login-status"), "");
  try {
    const session = await api("/api/session", "POST", { code: byId("access-code").value });
    byId("access-code").value = "";
    showSession(true, session);
    if (character.name) messageField.focus();
    else openProfile();
  } catch (error) {
    setStatus(byId("login-status"), error.message, true);
  } finally {
    button.disabled = false;
  }
});

byId("logout").addEventListener("click", async ({ currentTarget: button }) => {
  button.disabled = true;
  try {
    await api("/api/session", "DELETE");
    showSession(false);
    byId("access-code").focus();
  } catch (error) {
    handleError(error, messageStatus);
  } finally {
    button.disabled = false;
  }
});

byId("profile-button").addEventListener("click", openProfile);
byId("cancel-profile").addEventListener("click", () => profileDialog.close());
byId("profile-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const name = byId("character-name").value.trim();
  if (!name) return;
  character.name = name;
  storeLocal(characterKey, character);
  updateProfile();
  profileDialog.close();
  messageField.focus();
});

byId("message-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!character.name) {
    openProfile();
    return;
  }
  const button = event.submitter;
  const text = messageField.value.trim();
  if (!text || button.disabled) return;
  if (draftTarget !== undefined && draftTarget !== activeTarget) {
    setStatus(messageStatus, "Чат сменился. Измените черновик, чтобы отправить его в новый чат.", true);
    return;
  }
  button.disabled = true;
  setStatus(messageStatus, "");
  try {
    await api("/api/chat", "POST", { name: character.name, character_id: character.id, text, target_id: activeTarget });
    if (messageField.value.trim() === text) {
      messageField.value = "";
      draftTarget = undefined;
    }
    await refreshFeed(true);
  } catch (error) {
    if (error.status === 409) await refreshFeed(true);
    handleError(error, messageStatus);
  } finally {
    button.disabled = false;
  }
});

messageField.addEventListener("input", () => {
  draftTarget = messageField.value.trim() ? activeTarget : undefined;
  setStatus(messageStatus, "");
});

messageField.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    byId("message-form").requestSubmit(byId("message-form").querySelector('button[type="submit"]'));
  }
});

byId("favorite-form").addEventListener("submit", (event) => {
  event.preventDefault();
  favoriteDraft.description = descriptionField.value.trim();
  favoriteDialog.close();
  saveFavorites();
});
byId("cancel-favorite").addEventListener("click", () => favoriteDialog.close());
byId("open-saved").addEventListener("click", () => toggleDrawer(true));
byId("close-saved").addEventListener("click", () => toggleDrawer(false, true));
byId("drawer-backdrop").addEventListener("click", () => toggleDrawer(false, true));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !favoriteDialog.open && !profileDialog.open) toggleDrawer(false, true);
});

function showAdminSettings({ target_id }) {
  byId("admin-login-form").hidden = true;
  byId("admin-settings-form").hidden = false;
  targetField.value = target_id ?? "";
  showPrivateWarning();
  targetField.focus();
}

function showAdminLogin() {
  byId("admin-login-form").hidden = false;
  byId("admin-settings-form").hidden = true;
  byId("admin-code").focus();
}

function showPrivateWarning() {
  byId("private-warning").hidden = !/^\d+$/.test(targetField.value.trim()) || Number(targetField.value) === 0;
}

byId("admin-button").addEventListener("click", async () => {
  byId("admin-login-form").hidden = true;
  byId("admin-settings-form").hidden = true;
  setStatus(adminStatus, "");
  adminDialog.showModal();
  try {
    showAdminSettings(await api("/api/admin/chat"));
  } catch (error) {
    if (error.status === 403) showAdminLogin();
    else handleError(error, adminStatus);
  }
});

byId("admin-login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  setStatus(adminStatus, "");
  try {
    const settings = await api("/api/admin/session", "POST", { code: byId("admin-code").value });
    byId("admin-code").value = "";
    showAdminSettings(settings);
  } catch (error) {
    handleError(error, adminStatus);
  } finally {
    button.disabled = false;
  }
});

byId("admin-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  setStatus(adminStatus, "");
  try {
    const settings = await api("/api/admin/chat", "PUT", { target_id: targetField.value.trim() || null });
    showAdminSettings(settings);
    await refreshFeed(true);
    setStatus(adminStatus, "Настройка сохранена.");
  } catch (error) {
    if (error.status === 403) showAdminLogin();
    handleError(error, adminStatus);
  } finally {
    button.disabled = false;
  }
});
targetField.addEventListener("input", showPrivateWarning);
byId("close-admin").addEventListener("click", () => adminDialog.close());
adminDialog.addEventListener("close", () => { byId("admin-code").value = ""; });

api("/api/session")
  .then((session) => showSession(session.authenticated, session))
  .catch(() => {
    showSession(false);
    setStatus(byId("login-status"), "Сайт временно недоступен. Попробуйте войти ещё раз.", true);
  });

setInterval(() => {
  if (!document.hidden) refreshFeed();
}, 3000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refreshFeed();
});
