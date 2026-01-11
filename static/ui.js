function openModal(id) {
  const m = document.getElementById(id);
  if (!m) return;
  m.classList.add("open");
  m.setAttribute("aria-hidden", "false");
}

function closeModal(modal) {
  modal.classList.remove("open");
  modal.setAttribute("aria-hidden", "true");
}

document.addEventListener("click", (e) => {
  const openBtn = e.target.closest("[data-open]");
  if (openBtn) {
    const target = openBtn.getAttribute("data-open");
    openModal(target);

    // Preview modal fill
    if (target === "modalPreview") {
      const src = openBtn.getAttribute("data-src");
      const title = openBtn.getAttribute("data-title") || "Vista";
      const project = openBtn.getAttribute("data-project") || "";
      const desc = openBtn.getAttribute("data-desc") || "";
      const date = openBtn.getAttribute("data-date") || "";

      document.getElementById("pvImg").src = src;
      document.getElementById("pvTitle").textContent = title;
      document.getElementById("pvMeta").textContent = `Proyecto: ${project} • Fecha: ${date}`;
      document.getElementById("pvDesc").textContent = desc;
    }

    // Edit modal fill
    if (target === "modalEdit") {
      const id = openBtn.getAttribute("data-id");
      const display = openBtn.getAttribute("data-display") || "";
      const desc = openBtn.getAttribute("data-desc") || "";
      const projectId = openBtn.getAttribute("data-projectid") || "";
      const img = openBtn.getAttribute("data-img");

      const form = document.getElementById("editForm");
      form.action = `/photos/${id}/edit`;

      document.getElementById("editDisplay").value = display;
      document.getElementById("editDesc").value = desc;
      document.getElementById("editProject").value = projectId;
      document.getElementById("editImg").src = img;
    }

    return;
  }

  const closeBtn = e.target.closest("[data-close]");
  if (closeBtn) {
    const modal = closeBtn.closest(".modal");
    if (modal) closeModal(modal);
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    document.querySelectorAll(".modal.open").forEach(closeModal);
  }
});

document.addEventListener("submit", (e) => {
  const form = e.target.closest("form[data-confirm-message]");
  if (!form) return;

  e.preventDefault();

  const message = form.getAttribute("data-confirm-message") || "¿Seguro que deseas continuar?";
  const modal = document.getElementById("modalConfirm");

  if (!modal) {
    if (window.confirm(message)) {
      form.submit();
    }
    return;
  }

  const textEl = modal.querySelector(".confirm-message");
  if (textEl) {
    textEl.textContent = message;
  }

  const key = String(Date.now());
  modal.dataset.confirmKey = key;
  form.dataset.confirmKey = key;

  openModal("modalConfirm");
});

document.addEventListener("click", (e) => {
  const acceptBtn = e.target.closest("[data-confirm-accept]");
  if (!acceptBtn) return;

  const modal = acceptBtn.closest(".modal");
  if (!modal) return;

  const key = modal.dataset.confirmKey;
  if (!key) {
    closeModal(modal);
    return;
  }

  const form = document.querySelector('form[data-confirm-key="' + key + '"]');
  if (form) {
    form.removeAttribute("data-confirm-key");
    closeModal(modal);
    form.submit();
  } else {
    closeModal(modal);
  }
});

// Transform legacy inline confirm() handlers into data-confirm-message
document.addEventListener("DOMContentLoaded", () => {
  const legacyForms = document.querySelectorAll('form[onsubmit*="confirm("]');
  legacyForms.forEach((form) => {
    const handler = form.getAttribute("onsubmit") || "";
    const match = handler.match(/confirm\('([^']*)'\)/);
    const message = match && match[1] ? match[1] : "¿Seguro que deseas continuar?";

    // Attach as data-confirm-message for our custom modal system
    if (!form.hasAttribute("data-confirm-message")) {
      form.setAttribute("data-confirm-message", message);
    }

    form.removeAttribute("onsubmit");
    form.onsubmit = null;
  });
});
