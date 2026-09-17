const PAGE_STATES = Object.freeze({
  INITIAL: "initial",
  LOADING: "loading",
  SUCCESS: "success",
  MULTIPLE: "multiple",
  NOT_FOUND: "not-found",
  LOAD_ERROR: "load-error",
  NO_DATA: "no-data",
});

const STATE_MESSAGES = {
  [PAGE_STATES.INITIAL]: "請輸入關鍵字開始搜尋",
  [PAGE_STATES.LOADING]: "商品資料載入中…",
  [PAGE_STATES.SUCCESS]: "",
  [PAGE_STATES.MULTIPLE]: "請選擇商品查看完整資訊",
  [PAGE_STATES.NOT_FOUND]: "找不到符合的食品資料",
  [PAGE_STATES.LOAD_ERROR]: "食品資料目前無法載入，請稍後再試。",
  [PAGE_STATES.NO_DATA]: "目前沒有可用的食品資料。",
};

function renderPageState(elements, state, count = null) {
  elements.section.dataset.state = state;
  elements.section.setAttribute("aria-busy", String(state === PAGE_STATES.LOADING));
  elements.status.textContent = STATE_MESSAGES[state];
  elements.count.textContent = count === null ? "" : `${count} 筆`;
  elements.results.replaceChildren();
  const disabled = [PAGE_STATES.LOADING, PAGE_STATES.LOAD_ERROR, PAGE_STATES.NO_DATA].includes(state);
  elements.input.disabled = disabled;
  elements.button.disabled = disabled;
}

function renderProductList(container, matches, onSelect) {
  const list = document.createElement("ul");
  list.className = "product-list";
  matches.forEach((product) => {
    const item = document.createElement("li");
    const button = document.createElement("button");
    const name = document.createElement("strong");
    const summary = document.createElement("span");
    button.type = "button";
    button.className = "product-option";
    name.textContent = displayValue(product.name);
    summary.textContent = `品牌：${displayValue(product.brand)} · 條碼：${displayValue(product.barcode)}`;
    button.append(name, summary);
    button.addEventListener("click", () => onSelect(product));
    item.append(button);
    list.append(item);
  });
  container.replaceChildren(list);
}
