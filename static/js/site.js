document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector(".nav-toggle");
  const nav = document.querySelector(".nav-links");
  if (toggle && nav) toggle.addEventListener("click", () => {
    const open = nav.classList.toggle("open");
    toggle.setAttribute("aria-expanded", String(open));
  });

  document.querySelectorAll(".duration-btn").forEach(button => {
    button.addEventListener("click", () => {
      const duration = button.dataset.duration;
      document.querySelectorAll(".duration-btn").forEach(b => {
        const active = b === button;
        b.classList.toggle("active", active);
        b.setAttribute("aria-pressed", String(active));
      });
      document.querySelectorAll(".inr-price").forEach(el => {
        el.textContent = "₹" + Number(el.dataset[duration]).toLocaleString("en-IN");
      });
      document.querySelectorAll(".usd-price").forEach(el => {
        el.textContent = "$" + Number(el.dataset[duration]).toFixed(2);
      });
      document.querySelectorAll(".duration-label").forEach(el => {
        el.textContent = "/ " + (duration === "yearly" ? "year" : duration === "monthly" ? "month" : "week");
      });
    });
  });
});
