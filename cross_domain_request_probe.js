/*
 * Cross-domain XHR/Fetch probe for submit diagnostics.
 *
 * Privacy boundary:
 * - records only target origin/hostname, method, type, status and timing
 * - never records full URLs, query strings, headers, request bodies or responses
 * - ignores same-origin requests by default
 *
 * Retry safety:
 * - retries GET/HEAD/OPTIONS by default
 * - POST/PUT/PATCH/DELETE are never retried unless explicitly enabled
 *   because a submit retry can duplicate a business operation
 */
(() => {
  const KEY = '__gg88CrossDomainRequestProbe';
  const DEFAULTS = {
    maxRetries: 2,
    retryDelayMs: 100,
    retryMethods: ['GET', 'HEAD', 'OPTIONS'],
    retryUnsafeMethods: false,
  };

  const normalizeMethod = (value) => String(value || 'GET').toUpperCase().slice(0, 16);
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));

  const describeTarget = (rawUrl) => {
    try {
      const url = new URL(String(rawUrl || ''), window.location.href);
      if (url.origin === window.location.origin) return null;
      return {
        origin: url.origin,
        hostname: url.hostname,
        protocol: url.protocol,
        port: url.port || '',
        // url is intentionally not returned in events; it remains private to
        // the in-page retry implementation.
        url: url.href,
      };
    } catch (_) {
      return null;
    }
  };

  const classifyFailure = ({type, status, error, ok}) => {
    if (ok === true && status >= 200 && status < 400) return 'success';
    if (type === 'xhr' && error === 'aborted') return 'aborted';
    if (type === 'xhr' && error === 'timeout') return 'timeout';
    if (status >= 400) return 'http_error';
    if (status === 0 && (error === 'TypeError' || error === 'network-error')) {
      return 'cors_or_network_unknown';
    }
    if (status === 0) return 'network_unknown';
    return 'response_not_ok';
  };

  const install = (options = {}) => {
    const existing = window[KEY];
    if (existing && existing.active) return {active: true, reused: true};

    const config = {
      ...DEFAULTS,
      ...options,
      maxRetries: Math.max(0, Math.min(5, Number(options.maxRetries ?? DEFAULTS.maxRetries))),
      retryDelayMs: Math.max(0, Math.min(2000, Number(options.retryDelayMs ?? DEFAULTS.retryDelayMs))),
      retryMethods: Array.isArray(options.retryMethods)
        ? options.retryMethods.map(normalizeMethod)
        : DEFAULTS.retryMethods,
    };
    const state = {
      active: true,
      events: [],
      config,
      originalFetch: window.fetch,
      originalOpen: XMLHttpRequest.prototype.open,
      originalSend: XMLHttpRequest.prototype.send,
    };

    const canRetry = (method) => {
      const normalized = normalizeMethod(method);
      if (config.retryMethods.includes(normalized)) return true;
      return config.retryUnsafeMethods === true;
    };

    const record = (entry) => {
      if (!state.active || !entry.target) return;
      const failureClass = classifyFailure(entry);
      state.events.push({
        type: entry.type,
        method: normalizeMethod(entry.method),
        origin: entry.target.origin,
        hostname: entry.target.hostname,
        protocol: entry.target.protocol,
        port: entry.target.port,
        status: Number.isFinite(entry.status) ? entry.status : 0,
        ok: entry.ok === true,
        failure_class: failureClass,
        retry_count: Math.max(0, Number(entry.retryCount) || 0),
        retryable: canRetry(entry.method),
        error: entry.error ? String(entry.error).slice(0, 80) : '',
        duration_ms: Math.round(Math.max(0, performance.now() - entry.startedAt) * 100) / 100,
        at: new Date().toISOString(),
      });
    };

    const fetchWithRetry = async (input, init, meta, retryCount = 0) => {
      const startedAt = performance.now();
      try {
        const response = await state.originalFetch.apply(window, [input, init]);
        const method = meta.method;
        const failed = !response.ok || response.status >= 400;
        if (failed && canRetry(method) && retryCount < config.maxRetries && state.active) {
          await sleep(config.retryDelayMs * (retryCount + 1));
          return fetchWithRetry(input, init, meta, retryCount + 1);
        }
        record({
          type: 'fetch', method, target: meta.target, startedAt,
          status: response.status, ok: response.ok, retryCount,
        });
        return response;
      } catch (error) {
        if (canRetry(meta.method) && retryCount < config.maxRetries && state.active) {
          await sleep(config.retryDelayMs * (retryCount + 1));
          return fetchWithRetry(input, init, meta, retryCount + 1);
        }
        record({
          type: 'fetch', method: meta.method, target: meta.target,
          startedAt, status: 0, error: error && error.name, retryCount,
        });
        throw error;
      }
    };

    window.fetch = function patchedFetch(input, init) {
      const rawUrl = typeof input === 'string' ? input : input && input.url;
      const target = describeTarget(rawUrl);
      const method = normalizeMethod((init && init.method) || (input && input.method));
      if (!target) return state.originalFetch.apply(this, arguments);
      return fetchWithRetry(input, init, {method, target});
    };

    XMLHttpRequest.prototype.open = function patchedOpen(method, url) {
      this.__gg88ProbeMeta = {
        method: normalizeMethod(method),
        target: describeTarget(url),
        url: String(url || ''),
        async: arguments.length < 3 || arguments[2] !== false,
      };
      return state.originalOpen.apply(this, arguments);
    };

    const retryXhr = (meta, retryCount, startedAt, finish) => {
      const xhr = new XMLHttpRequest();
      let settled = false;
      const complete = (error = '') => {
        if (settled) return;
        settled = true;
        const status = Number(xhr.status) || 0;
        const ok = status >= 200 && status < 300;
        const failure = classifyFailure({type: 'xhr', status, error, ok});
        if (failure !== 'success' && canRetry(meta.method)
            && retryCount < config.maxRetries && state.active) {
          setTimeout(() => retryXhr(meta, retryCount + 1, startedAt, finish),
            config.retryDelayMs * (retryCount + 1));
          return;
        }
        finish({status, ok, error, retryCount});
      };
      try {
        xhr.open(meta.method, meta.url, meta.async);
        xhr.addEventListener('loadend', () => complete(''), {once: true});
        xhr.addEventListener('error', () => complete('network-error'), {once: true});
        xhr.addEventListener('abort', () => complete('aborted'), {once: true});
        xhr.addEventListener('timeout', () => complete('timeout'), {once: true});
        xhr.send(null); // unsafe/body-bearing XHRs are not retried by default
      } catch (error) {
        complete(error && error.name ? error.name : 'network-error');
      }
    };

    XMLHttpRequest.prototype.send = function patchedSend() {
      const meta = this.__gg88ProbeMeta || {};
      if (!meta.target) return state.originalSend.apply(this, arguments);
      const startedAt = performance.now();
      const originalXhr = this;
      let finished = false;
      const finish = ({status, ok, error, retryCount}) => {
        if (finished || !state.active) return;
        finished = true;
        record({
          type: 'xhr', method: meta.method, target: meta.target, startedAt,
          status, ok, error, retryCount,
        });
      };
      const completeOriginal = (error = '') => {
        if (finished) return;
        const status = Number(originalXhr.status) || 0;
        const ok = status >= 200 && status < 300;
        const failure = classifyFailure({type: 'xhr', status, error, ok});
        if (failure !== 'success' && canRetry(meta.method)
            && config.maxRetries > 0 && state.active) {
          setTimeout(() => retryXhr(meta, 1, startedAt, finish), config.retryDelayMs);
          return;
        }
        finish({status, ok, error, retryCount: 0});
      };
      originalXhr.addEventListener('loadend', () => completeOriginal(''), {once: true});
      originalXhr.addEventListener('error', () => completeOriginal('network-error'), {once: true});
      originalXhr.addEventListener('abort', () => completeOriginal('aborted'), {once: true});
      originalXhr.addEventListener('timeout', () => completeOriginal('timeout'), {once: true});
      return state.originalSend.apply(this, arguments);
    };

    state.stop = () => {
      if (!state.active) return [];
      state.active = false;
      window.fetch = state.originalFetch;
      XMLHttpRequest.prototype.open = state.originalOpen;
      XMLHttpRequest.prototype.send = state.originalSend;
      const events = state.events.slice();
      delete window[KEY];
      return events;
    };
    window[KEY] = state;
    return {active: true, maxRetries: config.maxRetries, retryDelayMs: config.retryDelayMs};
  };

  window.GG88RequestProbe = {
    start: install,
    stop: () => (window[KEY] && window[KEY].stop ? window[KEY].stop() : []),
  };
})();
