const INGREDIENTS_EMPTY_MESSAGE = "目前尚無食品成分資料";

function isIngredientObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function formatIngredientRatio(ratio) {
  if (!isIngredientObject(ratio) ||
      !Number.isFinite(ratio.value) || ratio.value < 0 ||
      !hasDisplayValue(ratio.unit)) {
    return null;
  }

  const unit = ratio.unit.trim();
  const separator = unit === "%" || unit === "％" ? "" : " ";
  return `${ratio.value}${separator}${unit}`;
}

function createIngredientSourceLink(source) {
  if (!hasDisplayValue(source?.name) || !hasDisplayValue(source?.url)) return null;

  try {
    const url = new URL(source.url);
    if (!['https:', 'http:'].includes(url.protocol)) return null;

    const link = document.createElement("a");
    link.className = "source-link";
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = source.name.trim();
    return link;
  } catch {
    return null;
  }
}

function getIngredientSources(product, ingredients) {
  const sourceIds = [];

  ingredients.forEach((ingredient) => {
    if (!hasDisplayValue(ingredient.sourceId)) return;
    const sourceId = ingredient.sourceId.trim();
    if (!sourceIds.includes(sourceId)) sourceIds.push(sourceId);
  });

  return sourceIds
    .map((sourceId) => resolveProductSource(product, sourceId))
    .filter((source) => source !== null);
}

function createIngredientsSection(product = {}) {
  const section = document.createElement("section");
  const title = document.createElement("h4");
  const ingredients = Array.isArray(product.ingredients)
    ? product.ingredients.filter((ingredient) =>
      isIngredientObject(ingredient) && hasDisplayValue(ingredient.name))
    : [];

  section.className = "ingredients-section";
  title.textContent = "食品成分";
  section.append(title);

  if (ingredients.length === 0) {
    const emptyMessage = document.createElement("p");
    emptyMessage.className = "ingredients-empty";
    emptyMessage.textContent = INGREDIENTS_EMPTY_MESSAGE;
    section.append(emptyMessage);
    return section;
  }

  const list = document.createElement("ol");
  list.className = "ingredients-list";
  ingredients.forEach((ingredient) => {
    const item = document.createElement("li");
    const name = document.createElement("span");
    const ratio = formatIngredientRatio(ingredient.ratio);

    item.className = "ingredient-item";
    name.className = "ingredient-name";
    name.textContent = ingredient.name.trim();
    item.append(name);

    if (ratio !== null) {
      const ratioText = document.createElement("span");
      ratioText.className = "ingredient-ratio";
      ratioText.textContent = ratio;
      item.append(ratioText);
    }

    list.append(item);
  });
  section.append(list);

  const sourceRow = document.createElement("p");
  const sources = getIngredientSources(product, ingredients);
  sourceRow.className = "ingredients-sources";
  sourceRow.append(document.createTextNode("資料來源："));

  const sourceLinks = sources
    .map(createIngredientSourceLink)
    .filter((link) => link !== null);

  if (sourceLinks.length === 0) {
    sourceRow.append(document.createTextNode("目前尚無資料"));
  } else {
    sourceLinks.forEach((link, index) => {
      if (index > 0) sourceRow.append(document.createTextNode("、"));
      sourceRow.append(link);
    });
  }
  section.append(sourceRow);

  return section;
}
