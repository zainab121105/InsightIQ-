const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

let authConfig = {
  getCsrfToken: () => null,
  getBusinessId: () => null,
  onCsrfToken: () => {},
  onAuthFailure: () => {},
};
let refreshPromise = null;
let csrfOperationSequence = 0;
let latestCsrfOperation = 0;

export function configureApiAuth(config) {
  authConfig = { ...authConfig, ...config };
}

function applyCsrfToken(token, operation) {
  if (!token || operation < latestCsrfOperation) return;
  latestCsrfOperation = operation;
  authConfig.onCsrfToken(token);
}

function withBusiness(path, includeBusiness) {
  if (!includeBusiness) return path;
  const businessId = authConfig.getBusinessId();
  if (!businessId) throw new Error("Select a business before continuing.");
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}business_id=${encodeURIComponent(businessId)}`;
}

async function responseError(response, fallback) {
  let detail = `${fallback} (status ${response.status})`;
  try {
    const body = await response.json();
    if (typeof body.detail === "string") detail = body.detail;
    else if (body.detail && typeof body.detail.message === "string") {
      detail = body.detail.retry_after_seconds != null
        ? `${body.detail.message} You can retry in about ${body.detail.retry_after_seconds} seconds.`
        : body.detail.message;
    }
  } catch {
    // Preserve the status-based message for non-JSON responses.
  }
  return detail;
}

async function refreshSession() {
  if (!refreshPromise) {
    const operation = ++csrfOperationSequence;
    refreshPromise = fetch(`${API_BASE_URL}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": authConfig.getCsrfToken() || "",
      },
    }).then(async (response) => {
      if (!response.ok) throw new Error(await responseError(response, "Session refresh failed"));
      const body = await response.json();
      if (!body.csrf_token) throw new Error("Session refresh returned no CSRF token.");
      applyCsrfToken(body.csrf_token, operation);
      return body;
    }).finally(() => {
      refreshPromise = null;
    });
  }

  return refreshPromise;
}

export const bootstrapCsrf = () => {
  const operation = ++csrfOperationSequence;
  return request("/auth/csrf", {
    method: "POST",
  }, { retry: false }).then((result) => {
    applyCsrfToken(result.csrf_token, operation);
    return result;
  });
};

async function request(path, options = {}, settings = {}) {
  const {
    includeBusiness = false,
    csrf = false,
    retry = true,
  } = settings;
  const headers = new Headers(options.headers || {});
  if (!(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (csrf) {
    const token = authConfig.getCsrfToken();
    if (!token) throw new Error("Your session needs to be refreshed.");
    headers.set("X-CSRF-Token", token);
  }

  const response = await fetch(
    `${API_BASE_URL}${withBusiness(path, includeBusiness)}`,
    { ...options, headers, credentials: "include" },
  );
  if (response.status === 401 && retry && !path.startsWith("/auth/")) {
    try {
      await refreshSession();
      return request(path, options, { ...settings, retry: false });
    } catch (error) {
      authConfig.onAuthFailure();
      throw error;
    }
  }
  if (!response.ok) throw new Error(await responseError(response, "Request failed"));
  return response.json();
}

export const getCurrentUser = () => request("/auth/me", {}, { retry: false });
export const getBusinesses = () => request("/auth/businesses", {}, { retry: false });

export const login = (payload) =>
  request("/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  }, { retry: false }).then((result) => {
    applyCsrfToken(result.csrf_token, ++csrfOperationSequence);
    return result;
  });

export const signup = (payload) =>
  request("/auth/signup", {
    method: "POST",
    body: JSON.stringify(payload),
  }, { retry: false }).then((result) => {
    applyCsrfToken(result.csrf_token, ++csrfOperationSequence);
    return result;
  });

export const logout = () =>
  request("/auth/logout", { method: "POST" }, { csrf: true, retry: false });

export const getSummary = () => request("/analytics/summary", {}, { includeBusiness: true });
export const getProducts = () => request("/analytics/products", {}, { includeBusiness: true });
export const getRegions = () => request("/analytics/regions", {}, { includeBusiness: true });
export const getMonthly = () => request("/analytics/monthly", {}, { includeBusiness: true });

export const uploadSalesPreview = (file) => {
  const formData = new FormData();
  formData.append("file", file);
  return request("/sales/upload-preview", {
    method: "POST",
    body: formData,
  }, { includeBusiness: true, csrf: true });
};

export const confirmSalesImport = (previewId) =>
  request("/sales/import-confirm", {
    method: "POST",
    body: JSON.stringify({ preview_id: previewId }),
  }, { includeBusiness: true, csrf: true });

export const askAssistant = (question, conversationId, previousMessages) =>
  request("/chat", {
    method: "POST",
    body: JSON.stringify({
      question,
      conversation_id: conversationId,
      messages: previousMessages,
    }),
  }, { includeBusiness: true, csrf: true });
