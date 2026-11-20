/**
 * Authentication and Header construction helpers for k6.
 * Guarantees zero hardcoded credentials and preserves tenant/namespace isolation.
 */

import { CLIENT_TOKEN, ADMIN_TOKEN } from '../config/test-profiles.js';

export function getClientHeaders(customToken, bypassCache = false) {
  const token = customToken || CLIENT_TOKEN;
  const headers = {
    'Content-Type': 'application/json',
    'Accept': 'application/json',
  };

  if (token) {
    headers['X-Client-Token'] = token;
  }

  if (bypassCache) {
    headers['X-Bypass-Cache'] = 'true';
  }

  return headers;
}

export function getAdminHeaders(customAdminToken) {
  const token = customAdminToken || ADMIN_TOKEN;
  const headers = {
    'Accept': 'application/json',
  };

  if (token) {
    headers['X-Admin-Token'] = token;
  }

  return headers;
}
