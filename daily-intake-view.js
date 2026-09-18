function createDailyIntakeView({ store, repository, container, emptyMessage, clearButton, totalsContainer }) {
  function createActionButton(label, action, item, productName) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.dataset.action = action;
    button.dataset.productId = item.productId;
    button.setAttribute("aria-label", `${productName}：${label}`);
    if (action === "decrease") button.disabled = item.quantity <= 1;
    return button;
  }

  function createItem(item) {
    const product = repository.getProductById(item.productId);
    const name = displayValue(product?.name);
    const row = document.createElement("li");
    const heading = document.createElement("h3");
    const details = document.createElement("dl");
    const actions = document.createElement("div");
    row.className = "daily-intake-item";
    row.dataset.productId = item.productId;
    heading.textContent = name;
    details.className = "product-details";
    const quantity = createDetail("數量", item.quantity);
    quantity.querySelector("dd").dataset.role = "quantity";
    details.append(
      createDetail("品牌", product?.brand),
      createDetail("每份規格", formatQuantity(product?.serving, "", "amount")),
      quantity,
    );
    actions.className = "daily-intake-actions";
    actions.append(
      createActionButton("減少數量", "decrease", item, name),
      createActionButton("增加數量", "increase", item, name),
      createActionButton("移除", "remove", item, name),
    );
    row.append(heading, details, actions);
    return row;
  }

  function render(focusTarget) {
    const items = store.getItems();
    const list = document.createElement("ul");
    list.className = "daily-intake-list";
    items.forEach((item) => list.append(createItem(item)));
    container.replaceChildren(list);
    emptyMessage.hidden = items.length > 0;
    clearButton.disabled = items.length === 0;
    renderDailyNutrition(totalsContainer, items, repository);
    if (focusTarget) {
      const button = Array.from(container.querySelectorAll("button")).find((candidate) =>
        candidate.dataset.productId === focusTarget.productId &&
        candidate.dataset.action === focusTarget.action,
      );
      // 減至 1 時減少按鈕停用，將焦點保留在同一項目的增加按鈕。
      const fallback = Array.from(container.querySelectorAll("button")).find((candidate) =>
        candidate.dataset.productId === focusTarget.productId && candidate.dataset.action === "increase",
      );
      const target = button && !button.disabled ? button : fallback;
      target?.focus();
    }
  }

  function addProduct(productId) {
    if (!repository.getProductById(productId) || !store.addProduct(productId)) return false;
    render();
    return true;
  }

  container.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button || !container.contains(button) || button.disabled) return;
    const { productId, action } = button.dataset;
    const item = store.getItems().find((entry) => entry.productId === productId);
    if (!item) return;
    if (action === "increase") store.addProduct(productId);
    else if (action === "decrease") store.updateQuantity(productId, Math.max(1, item.quantity - 1));
    else if (action === "remove") store.removeProduct(productId);
    else return;
    render({ productId, action });
  });

  clearButton.addEventListener("click", () => {
    store.clear();
    render();
  });

  return Object.freeze({ addProduct, render });
}
