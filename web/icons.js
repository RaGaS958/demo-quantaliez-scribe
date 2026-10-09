// Inline SVG icon sprite (stroke icons in the style of the deck). Usage: <svg class="i"><use href="#i-mic"/></svg>
(function () {
  const P = {
    mic: '<rect x="9" y="2.5" width="6" height="11.5" rx="3"/><path d="M5 10.5a7 7 0 0 0 14 0"/><path d="M12 17.5v3.5"/><path d="M8.5 21h7"/>',
    lang: '<path d="M4 5h7"/><path d="M9 3v2c0 4.4-2.2 8-5 8"/><path d="M5 9c0 2.1 3 3.9 6.7 4"/><path d="M12 20l4-9 4 9"/><path d="M19.1 18h-6.2"/>',
    steth: '<path d="M6 4H5a2 2 0 0 0-2 2v3.5a5.5 5.5 0 0 0 11 0V6a2 2 0 0 0-2-2h-1"/><path d="M8.5 15a6 6 0 0 0 11.5 0v-3"/><path d="M11 3v2"/><path d="M6 3v2"/><circle cx="20" cy="10" r="2"/>',
    file: '<path d="M14 3v4a1 1 0 0 0 1 1h4"/><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z"/><path d="M10 12.5l-1.5 2 1.5 2"/><path d="M14 12.5l1.5 2-1.5 2"/>',
    usercheck: '<circle cx="9" cy="7" r="4"/><path d="M3 21v-2a4 4 0 0 1 4-4h4"/><path d="M15 19l2 2 4-4"/>',
    shield: '<path d="M12 3a12 12 0 0 0 8.5 3A12 12 0 0 1 12 21 12 12 0 0 1 3.5 6 12 12 0 0 0 12 3"/><circle cx="12" cy="11" r="1"/><path d="M12 12v2.5"/>',
    wave: '<path d="M3 12h2.5l2-5 3 10 3-14 3 12 2-3H21"/>',
    pencil: '<path d="M4 20h4L18.5 9.5a2.83 2.83 0 0 0-4-4L4 16v4"/><path d="M13.5 6.5l4 4"/>',
    upload: '<path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/><path d="M7 9l5-5 5 5"/><path d="M12 4v12"/>',
    play: '<path d="M7 4v16l13-8z"/>',
    broadcast: '<path d="M18.36 19.36a9 9 0 1 0-12.72 0"/><path d="M15.54 16.54a5 5 0 1 0-7.08 0"/><circle cx="12" cy="13" r="1"/>',
    check: '<path d="M5 12l5 5L20 7"/>',
    alert: '<path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.24 3.96 2.4 17.06A2 2 0 0 0 4.11 20h15.78a2 2 0 0 0 1.71-2.94L13.76 3.96a2 2 0 0 0-3.52 0z"/>',
    history: '<path d="M12 8v4l2 2"/><path d="M3.05 11a9 9 0 1 1 .5 4"/><path d="M3 20v-5h5"/>',
    download: '<path d="M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/><path d="M7 11l5 5 5-5"/><path d="M12 4v12"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    left: '<path d="M5 12h14M5 12l6 6M5 12l6-6"/>',
    skip: '<path d="M4 5v14l8-7z"/><path d="M12 5v14l8-7z"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    exchange: '<path d="M7 10h14l-4-4"/><path d="M17 14H3l4 4"/>',
    id: '<rect x="3" y="4" width="18" height="16" rx="3"/><circle cx="9" cy="10" r="2"/><path d="M15 8h2M15 12h2M7 16h10"/>',
    lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><circle cx="12" cy="16" r="1"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/>',
  };
  const svg = Object.entries(P)
    .map(([k, v]) => `<symbol id="i-${k}" viewBox="0 0 24 24">${v}</symbol>`)
    .join("");
  const holder = document.createElement("div");
  holder.style.display = "none";
  holder.innerHTML = `<svg xmlns="http://www.w3.org/2000/svg">${svg}</svg>`;
  (document.body || document.documentElement).prepend(holder);
})();
