// 必須明確引用來源，不以第一筆來源代替不存在的來源 id。
function resolveProductSource(product, sourceId) {
  if (typeof sourceId !== "string" || sourceId.trim() === "" ||
      !Array.isArray(product?.sources)) return undefined;
  return product.sources.find((source) =>
    source !== null && typeof source === "object" && !Array.isArray(source) &&
    source.id === sourceId,
  );
}
