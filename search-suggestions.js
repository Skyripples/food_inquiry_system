function createSearchSuggestions({ input, container, repository, onSelect }) {
  let matches = [];
  let activeIndex = -1;

  function getOptions() {
    return Array.from(container.querySelectorAll("[role='option']"));
  }

  function setExpanded(expanded) {
    input.setAttribute("aria-expanded", String(expanded));
    container.hidden = !expanded;
    if (!expanded) input.removeAttribute("aria-activedescendant");
  }

  function setActiveIndex(index) {
    const options = getOptions();
    options.forEach((option) => {
      option.classList.remove("is-active");
      option.setAttribute("aria-selected", "false");
    });
    if (options.length === 0) {
      activeIndex = -1;
      input.removeAttribute("aria-activedescendant");
      return;
    }
    activeIndex = (index + options.length) % options.length;
    const activeOption = options[activeIndex];
    activeOption.classList.add("is-active");
    activeOption.setAttribute("aria-selected", "true");
    input.setAttribute("aria-activedescendant", activeOption.id);
    activeOption.scrollIntoView({ block: "nearest" });
  }

  function close() {
    matches = [];
    activeIndex = -1;
    container.replaceChildren();
    setExpanded(false);
  }

  function choose(index) {
    const product = matches[index];
    if (!product) return;
    input.value = product.name;
    close();
    onSelect(product);
  }

  function render() {
    const keyword = input.value.trim();
    if (!keyword) {
      close();
      return;
    }

    matches = repository.searchProducts(keyword);
    activeIndex = -1;
    container.replaceChildren();
    matches.forEach((product, index) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      const name = document.createElement("strong");
      const details = document.createElement("span");
      item.setAttribute("role", "presentation");
      button.id = `search-suggestion-${index}`;
      button.type = "button";
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", "false");
      button.dataset.index = String(index);
      name.textContent = displayValue(product.name);
      details.textContent = `${displayValue(product.brand)} · ${displayValue(product.barcode)}`;
      button.append(name, details);
      item.append(button);
      container.append(item);
    });
    setExpanded(matches.length > 0);
  }

  input.addEventListener("input", render);
  input.addEventListener("focus", render);
  input.addEventListener("keydown", (event) => {
    if (container.hidden && !["ArrowDown", "ArrowUp"].includes(event.key)) return;
    if (["ArrowDown", "ArrowUp"].includes(event.key)) {
      if (container.hidden) render();
      if (matches.length === 0) return;
      event.preventDefault();
      const nextIndex = activeIndex < 0
        ? (event.key === "ArrowDown" ? 0 : matches.length - 1)
        : activeIndex + (event.key === "ArrowDown" ? 1 : -1);
      setActiveIndex(nextIndex);
    } else if (event.key === "Enter" && activeIndex >= 0) {
      event.preventDefault();
      choose(activeIndex);
    } else if (event.key === "Escape") {
      event.preventDefault();
      close();
    }
  });

  container.addEventListener("mousedown", (event) => event.preventDefault());
  container.addEventListener("click", (event) => {
    const option = event.target.closest("[role='option']");
    if (!option || !container.contains(option)) return;
    choose(Number(option.dataset.index));
  });

  document.addEventListener("click", (event) => {
    if (event.target !== input && !container.contains(event.target)) close();
  });

  return Object.freeze({ close, render });
}
