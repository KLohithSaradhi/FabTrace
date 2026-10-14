(() => {
  "use strict";

  const form = document.querySelector("#plan-order-form");
  if (!form) return;
  const status = document.querySelector("#plan-form-status");
  const result = document.querySelector("#plan-result");

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector("button[type='submit']");
    button.disabled = true;
    status.className = "form-status";
    status.textContent = "Planning…";
    result.hidden = true;
    const data = new FormData(form);
    const payload = {
      order_id: data.get("order_id"),
      product_id: form.dataset.productId,
      product_version: Number(form.dataset.productVersion),
      quantity: Number(data.get("quantity")),
      requested_at: data.get("requested_at"),
    };
    try {
      const response = await fetch("/api/v1/plans", {
        method: "POST",
        headers: {"Content-Type": "application/json", "X-FabTrace-Editor": "1"},
        body: JSON.stringify(payload),
      });
      const body = await response.json();
      if (!response.ok) {
        const detail = Array.isArray(body.detail)
          ? body.detail.map((item) => item.msg).join("; ")
          : body.detail;
        throw new Error(detail || `Planning failed (${response.status})`);
      }
      const plan = body.plan;
      status.className = "form-status success";
      status.textContent = "Plan stored. This order ID cannot be overwritten.";
      const providerRows = plan.provider_orders.map((order) =>
        `<tr><td><code>${escapeHtml(order.provider_order_id)}</code></td><td>${escapeHtml(order.provider_id)}</td><td>${order.quantity} ${escapeHtml(order.unit)}</td><td><time>${escapeHtml(order.expected_completion_at)}</time></td></tr>`
      ).join("");
      result.innerHTML = `<div class="panel-heading"><h2>Plan generated</h2><a class="button-link" href="/plans/${encodeURIComponent(plan.order_id)}">Open full plan</a></div><div class="plan-result-summary"><p><span>Expected delivery</span><strong>${escapeHtml(plan.expected_delivery_at)}</strong></p><p><span>Critical path</span><strong>${plan.critical_path_node_ids.map(escapeHtml).join(" → ")}</strong></p></div><div class="table-scroll"><table><thead><tr><th>Provider order</th><th>Provider</th><th>Quantity</th><th>Expected completion</th></tr></thead><tbody>${providerRows}</tbody></table></div>`;
      result.hidden = false;
    } catch (error) {
      status.className = "form-status error";
      status.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  function escapeHtml(value) {
    return String(value).replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[character]);
  }
})();
