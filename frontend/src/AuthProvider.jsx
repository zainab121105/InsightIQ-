import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  configureApiAuth,
  bootstrapCsrf,
  getBusinesses,
  getCurrentUser,
  login as apiLogin,
  logout as apiLogout,
  signup as apiSignup,
} from "./api";

const AuthContext = createContext(null);

function isValidBusiness(businesses, businessId) {
  return businesses.some((business) => business.business_id === businessId);
}

export function AuthProvider({ children }) {
  const csrfRef = useRef(null);
  const businessRef = useRef(null);
  const [csrfToken, setCsrfTokenState] = useState(null);
  const [user, setUser] = useState(null);
  const [businesses, setBusinesses] = useState([]);
  const [selectedBusinessId, setSelectedBusinessIdState] = useState(null);
  const [loading, setLoading] = useState(true);
  const [authError, setAuthError] = useState("");

  const clearAuthentication = useCallback(() => {
    csrfRef.current = null;
    setCsrfTokenState(null);
    businessRef.current = null;
    setUser(null);
    setBusinesses([]);
    setSelectedBusinessIdState(null);
  }, []);

  const setCsrfToken = useCallback((token) => {
    csrfRef.current = token || null;
    setCsrfTokenState(token || null);
  }, []);

  const setSelectedBusinessId = useCallback((businessId) => {
    const numericId = Number(businessId);
    if (!isValidBusiness(businesses, numericId)) {
      throw new Error("That business is not available to your account.");
    }
    businessRef.current = numericId;
    setSelectedBusinessIdState(numericId);
  }, [businesses]);

  configureApiAuth({
    getCsrfToken: () => csrfRef.current,
    getBusinessId: () => businessRef.current,
    onCsrfToken: setCsrfToken,
    onAuthFailure: clearAuthentication,
  });

  const loadSession = useCallback(async (preferredBusinessId = null) => {
    const [profileResponse, businessResponse] = await Promise.all([
      getCurrentUser(),
      getBusinesses(),
    ]);
    const availableBusinesses = Array.isArray(businessResponse) ? businessResponse : [];
    setUser(profileResponse.user);
    setBusinesses(availableBusinesses);
    const preferred = Number(preferredBusinessId);
    const nextBusinessId = availableBusinesses.length === 1
      ? availableBusinesses[0].business_id
      : isValidBusiness(availableBusinesses, preferred)
        ? preferred
        : null;
    businessRef.current = nextBusinessId;
    setSelectedBusinessIdState(nextBusinessId);
    return { user: profileResponse.user, businesses: availableBusinesses };
  }, []);

  useEffect(() => {
    let active = true;
    bootstrapCsrf().then(() => loadSession()).catch(() => {
      if (active) clearAuthentication();
    }).finally(() => {
      if (active) setLoading(false);
    });
    return () => { active = false; };
  }, [clearAuthentication, loadSession]);

  const completeAuthentication = useCallback(async (result, preferredBusinessId) => {
    setCsrfToken(result.csrf_token);
    setAuthError("");
    await loadSession(preferredBusinessId);
  }, [loadSession, setCsrfToken]);

  const login = useCallback(async (payload) => {
    const result = await apiLogin(payload);
    await completeAuthentication(result);
  }, [completeAuthentication]);

  const signup = useCallback(async (payload) => {
    const result = await apiSignup(payload);
    await completeAuthentication(result, result.business_id);
  }, [completeAuthentication]);

  const logout = useCallback(async () => {
    try {
      if (csrfRef.current) await apiLogout();
    } finally {
      clearAuthentication();
    }
  }, [clearAuthentication]);

  const value = useMemo(() => ({
    user,
    businesses,
    selectedBusinessId,
    setSelectedBusinessId,
    csrfToken,
    loading,
    authenticated: Boolean(user),
    authError,
    setAuthError,
    login,
    signup,
    logout,
  }), [
    user,
    businesses,
    selectedBusinessId,
    csrfToken,
    setSelectedBusinessId,
    loading,
    authError,
    login,
    signup,
    logout,
  ]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used within AuthProvider.");
  return context;
}
