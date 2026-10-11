export const languageOptions = [
  { code: "en", label: "English" }, { code: "es", label: "Español" },
  { code: "fr", label: "Français" }, { code: "zh-Hans", label: "简体中文" },
] as const;
export type PresentationLanguage = typeof languageOptions[number]["code"];

const en = {
  Portfolio: "Portfolio", Research: "Research", Monitor: "Monitor", Knowledge: "Knowledge", Review: "Review", Simulate: "Simulate",
  Positions: "Positions", History: "History", Risk: "Risk", Decisions: "Decisions", Sources: "Sources", Ideas: "Ideas", Activity: "Activity", Settings: "Settings",
  More: "More", "Portfolio records": "Portfolio records", "Research library": "Research library", System: "System",
  language: "Presentation language", coverage: "Navigation, language controls and the evidence translation panel are localized. Other UI copy and deterministic fallback answers remain in English. Original evidence keeps its source language.",
  generation: "New supported chat answers and research summaries use this preference. Schema keys, citations and financial identifiers stay unchanged. Save settings to apply.",
  unavailable: "Language settings could not be refreshed; the last loaded language is shown.",
  translation: "Translated source view", original: "Read unchanged original", translate: "Translate with configured provider", working: "Translating…",
  cost: "Cached translations are free to read. Generating one sends this stored text to your configured model provider, using its account, quotas and possible charges. No paid or local fallback is selected automatically.",
  caution: "Machine translation is a presentation aid. Protected citations, identifiers and numeric spans are checked; meaning and omissions still require comparison with the original. This view does not update knowledge or conclusions.",
  loading: "Checking retained translation…", untranslated: "This source has no translation in the selected language.", stale: "The source version or translation checks changed. Its older translation is retained but is not shown as current.",
  provider_deferred: "Translation is deferred while the configured provider is unavailable. Recovery and configuration are in Settings.",
  provider_failed: "The configured provider could not translate this source. The original remains available.", fidelity_failed: "The translation did not preserve the protected source spans and was rejected.",
  source_too_large: "This source is too large for the bounded translation request. Read the unchanged original.", empty_source: "The stored source text is empty.",
  cached: "Retained translation of this source version", translated: "New translation of this source version", failed: "The source or translation could not be loaded. Retry or read the original.", retry: "Retry", version: "Source version",
};
const es = {
  Portfolio: "Cartera", Research: "Investigación", Monitor: "Seguimiento", Knowledge: "Conocimiento", Review: "Revisión", Simulate: "Simular",
  Positions: "Posiciones", History: "Historial", Risk: "Riesgo", Decisions: "Decisiones", Sources: "Fuentes", Ideas: "Ideas", Activity: "Actividad", Settings: "Configuración",
  More: "Más", "Portfolio records": "Registros de cartera", "Research library": "Biblioteca de investigación", System: "Sistema",
  language: "Idioma de presentación", coverage: "La navegación, los controles de idioma y el panel de traducción están traducidos. El resto de la interfaz y las respuestas de respaldo deterministas siguen en inglés. La evidencia original conserva su idioma.",
  generation: "Las nuevas respuestas de chat y los resúmenes compatibles usan esta preferencia. Las claves, citas e identificadores financieros se conservan. Guarde la configuración para aplicarla.",
  unavailable: "No se pudo actualizar el idioma; se muestra el último idioma cargado.",
  translation: "Vista traducida de la fuente", original: "Leer original sin cambios", translate: "Traducir con el proveedor configurado", working: "Traduciendo…",
  cost: "Leer traducciones guardadas no cuesta nada. Generarlas envía el texto al proveedor de modelos configurado y utiliza su cuenta, cuotas y posibles cargos. No se selecciona automáticamente otro proveedor de pago o local.",
  caution: "La traducción automática ayuda a leer. Se comprueban las citas, identificadores y números protegidos; el significado y las omisiones requieren comparación con el original. Esta vista no modifica conocimientos ni conclusiones.",
  loading: "Consultando traducción guardada…", untranslated: "No hay traducción en el idioma seleccionado.", stale: "La versión de la fuente o las comprobaciones de traducción cambiaron. Se conserva la traducción anterior, pero no se presenta como actual.",
  provider_deferred: "La traducción espera porque el proveedor no está disponible. Revise la configuración y recuperación.", provider_failed: "El proveedor no pudo traducir la fuente. El original sigue disponible.", fidelity_failed: "La traducción alteró elementos protegidos y fue rechazada.",
  source_too_large: "La fuente supera el límite de traducción. Lea el original sin cambios.", empty_source: "El texto original está vacío.",
  cached: "Traducción guardada de esta versión", translated: "Nueva traducción de esta versión", failed: "No se pudo cargar la fuente o traducción. Reintente o lea el original.", retry: "Reintentar", version: "Versión de la fuente",
};
const fr = {
  Portfolio: "Portefeuille", Research: "Recherche", Monitor: "Suivi", Knowledge: "Connaissances", Review: "Révision", Simulate: "Simuler",
  Positions: "Positions", History: "Historique", Risk: "Risque", Decisions: "Décisions", Sources: "Sources", Ideas: "Idées", Activity: "Activité", Settings: "Paramètres",
  More: "Plus", "Portfolio records": "Registres du portefeuille", "Research library": "Bibliothèque de recherche", System: "Système",
  language: "Langue de présentation", coverage: "La navigation, les contrôles de langue et le panneau de traduction sont traduits. Le reste de l’interface et les réponses de secours déterministes restent en anglais. Les sources conservent leur langue originale.",
  generation: "Les nouvelles réponses et synthèses prises en charge utilisent cette préférence. Les clés, citations et identifiants financiers sont conservés. Enregistrez les paramètres pour appliquer.",
  unavailable: "La langue n’a pas pu être actualisée ; la dernière langue chargée est affichée.",
  translation: "Vue traduite de la source", original: "Lire l’original inchangé", translate: "Traduire avec le fournisseur configuré", working: "Traduction…",
  cost: "La lecture des traductions conservées est gratuite. La génération envoie le texte au fournisseur configuré et utilise son compte, ses quotas et ses frais éventuels. Aucun autre fournisseur payant ou local n’est choisi automatiquement.",
  caution: "La traduction automatique facilite la lecture. Les citations, identifiants et nombres protégés sont vérifiés ; le sens et les omissions doivent être comparés à l’original. Cette vue ne modifie aucune connaissance ou conclusion.",
  loading: "Recherche de la traduction conservée…", untranslated: "Aucune traduction dans la langue choisie.", stale: "La version de la source ou les contrôles de traduction ont changé. L’ancienne traduction est conservée mais n’est pas présentée comme actuelle.",
  provider_deferred: "La traduction attend la disponibilité du fournisseur. Consultez les paramètres et la récupération.", provider_failed: "Le fournisseur n’a pas pu traduire cette source. L’original reste disponible.", fidelity_failed: "La traduction a modifié des éléments protégés et a été rejetée.",
  source_too_large: "Cette source dépasse la limite de traduction. Lisez l’original inchangé.", empty_source: "Le texte source est vide.",
  cached: "Traduction conservée de cette version", translated: "Nouvelle traduction de cette version", failed: "Impossible de charger la source ou sa traduction. Réessayez ou lisez l’original.", retry: "Réessayer", version: "Version de la source",
};
const zh = {
  Portfolio: "投资组合", Research: "研究", Monitor: "监测", Knowledge: "知识", Review: "审查", Simulate: "模拟",
  Positions: "持仓", History: "历史", Risk: "风险", Decisions: "决策", Sources: "来源", Ideas: "思路", Activity: "活动", Settings: "设置",
  More: "更多", "Portfolio records": "投资组合记录", "Research library": "研究资料库", System: "系统",
  language: "展示语言", coverage: "导航、语言设置和证据翻译面板已本地化。其他界面文字和确定性备用回答仍为英语。原始证据保留其原语言。",
  generation: "支持的新聊天回答和研究摘要使用此语言。结构字段、引用和金融标识保持不变。保存设置后生效。",
  unavailable: "语言设置刷新失败；显示上次加载的语言。",
  translation: "来源译文", original: "阅读未修改的原文", translate: "使用已配置的服务翻译", working: "正在翻译…",
  cost: "阅读缓存译文免费。生成译文会将存储的文本发送给已配置的模型服务，使用其账户和配额，并可能收费。不会自动切换到付费或本地服务。",
  caution: "机器翻译仅辅助阅读。受保护的引用、标识和数值已核对；含义和遗漏仍需对照原文。此视图不会修改知识或结论。",
  loading: "正在检查已保存的译文…", untranslated: "所选语言尚无译文。", stale: "来源版本或翻译检查已改变。旧译文仍保留，但不会作为当前版本显示。",
  provider_deferred: "已配置的服务不可用，翻译暂缓。请在设置中查看配置和恢复状态。", provider_failed: "服务未能翻译此来源。原文仍可阅读。", fidelity_failed: "译文未保留受保护的内容，已被拒绝。",
  source_too_large: "来源超过单次翻译长度上限。请阅读未修改的原文。", empty_source: "存储的原文为空。",
  cached: "此来源版本的已保存译文", translated: "此来源版本的新译文", failed: "无法加载来源或译文。请重试或阅读原文。", retry: "重试", version: "来源版本",
};
export const languageResources = { en: { translation: en }, es: { translation: es }, fr: { translation: fr }, "zh-Hans": { translation: zh } };
