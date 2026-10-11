import assert from "node:assert/strict";
import test from "node:test";
import { createInstance } from "i18next";
import { languageOptions, languageResources } from "../src/lib/language-resources.ts";

test("every supported UI language covers the declared navigation and translation controls", () => {
  const keys = Object.keys(languageResources.en.translation).sort();
  for (const language of languageOptions) {
    assert.deepEqual(Object.keys(languageResources[language.code].translation).sort(), keys);
    for (const value of Object.values(languageResources[language.code].translation)) assert.ok(value.trim());
  }
});

test("language switching and unsupported language fallback use retained English resources", async () => {
  const instance = createInstance();
  await instance.init({ resources: languageResources, lng: "es", fallbackLng: "en", initAsync: false });
  assert.equal(instance.t("Portfolio"), "Cartera");
  await instance.changeLanguage("zh-Hans");
  assert.equal(instance.t("Portfolio"), "投资组合");
  await instance.changeLanguage("unsupported");
  assert.equal(instance.t("Portfolio"), "Portfolio");
});
