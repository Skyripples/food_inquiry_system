// 每日參考值集中設定；沒有 dailyReference 的項目只顯示含量。
const NUTRITION_CONFIG = {
  calories: { label: "熱量", dailyReference: { amount: 2000, unit: "kcal" } },
  protein: { label: "蛋白質", dailyReference: { amount: 60, unit: "g" } },
  fat: { label: "脂肪", dailyReference: { amount: 60, unit: "g" } },
  saturatedFat: { label: "飽和脂肪", dailyReference: { amount: 18, unit: "g" } },
  transFat: { label: "反式脂肪" },
  carbohydrates: { label: "碳水化合物", dailyReference: { amount: 300, unit: "g" } },
  sugar: { label: "糖" },
  sodium: { label: "鈉", dailyReference: { amount: 2000, unit: "mg" } },
};
