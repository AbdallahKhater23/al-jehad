var __defProp = Object.defineProperty;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __esm = (fn, res) => function __init() {
  return fn && (res = (0, fn[__getOwnPropNames(fn)[0]])(fn = 0)), res;
};
var __export = (target, all) => {
  for (var name in all)
    __defProp(target, name, { get: all[name], enumerable: true });
};

// ../../mobile-client/node_modules/@capacitor/core/dist/index.js
var createCapacitorPlatforms, initPlatforms, CapacitorPlatforms, addPlatform, setPlatform, ExceptionCode, CapacitorException, getPlatformId, createCapacitor, initCapacitorGlobal, Capacitor, registerPlugin, Plugins, WebPlugin, encode, decode, CapacitorCookiesPluginWeb, CapacitorCookies, readBlobAsBase64, normalizeHttpHeaders, buildUrlParams, buildRequestInit, CapacitorHttpPluginWeb, CapacitorHttp;
var init_dist = __esm({
  "../../mobile-client/node_modules/@capacitor/core/dist/index.js"() {
    createCapacitorPlatforms = (win) => {
      const defaultPlatformMap = /* @__PURE__ */ new Map();
      defaultPlatformMap.set("web", { name: "web" });
      const capPlatforms = win.CapacitorPlatforms || {
        currentPlatform: { name: "web" },
        platforms: defaultPlatformMap
      };
      const addPlatform2 = (name, platform) => {
        capPlatforms.platforms.set(name, platform);
      };
      const setPlatform2 = (name) => {
        if (capPlatforms.platforms.has(name)) {
          capPlatforms.currentPlatform = capPlatforms.platforms.get(name);
        }
      };
      capPlatforms.addPlatform = addPlatform2;
      capPlatforms.setPlatform = setPlatform2;
      return capPlatforms;
    };
    initPlatforms = (win) => win.CapacitorPlatforms = createCapacitorPlatforms(win);
    CapacitorPlatforms = /* @__PURE__ */ initPlatforms(typeof globalThis !== "undefined" ? globalThis : typeof self !== "undefined" ? self : typeof window !== "undefined" ? window : typeof global !== "undefined" ? global : {});
    addPlatform = CapacitorPlatforms.addPlatform;
    setPlatform = CapacitorPlatforms.setPlatform;
    (function(ExceptionCode2) {
      ExceptionCode2["Unimplemented"] = "UNIMPLEMENTED";
      ExceptionCode2["Unavailable"] = "UNAVAILABLE";
    })(ExceptionCode || (ExceptionCode = {}));
    CapacitorException = class extends Error {
      constructor(message, code, data) {
        super(message);
        this.message = message;
        this.code = code;
        this.data = data;
      }
    };
    getPlatformId = (win) => {
      var _a, _b;
      if (win === null || win === void 0 ? void 0 : win.androidBridge) {
        return "android";
      } else if ((_b = (_a = win === null || win === void 0 ? void 0 : win.webkit) === null || _a === void 0 ? void 0 : _a.messageHandlers) === null || _b === void 0 ? void 0 : _b.bridge) {
        return "ios";
      } else {
        return "web";
      }
    };
    createCapacitor = (win) => {
      var _a, _b, _c, _d, _e;
      const capCustomPlatform = win.CapacitorCustomPlatform || null;
      const cap = win.Capacitor || {};
      const Plugins2 = cap.Plugins = cap.Plugins || {};
      const capPlatforms = win.CapacitorPlatforms;
      const defaultGetPlatform = () => {
        return capCustomPlatform !== null ? capCustomPlatform.name : getPlatformId(win);
      };
      const getPlatform = ((_a = capPlatforms === null || capPlatforms === void 0 ? void 0 : capPlatforms.currentPlatform) === null || _a === void 0 ? void 0 : _a.getPlatform) || defaultGetPlatform;
      const defaultIsNativePlatform = () => getPlatform() !== "web";
      const isNativePlatform = ((_b = capPlatforms === null || capPlatforms === void 0 ? void 0 : capPlatforms.currentPlatform) === null || _b === void 0 ? void 0 : _b.isNativePlatform) || defaultIsNativePlatform;
      const defaultIsPluginAvailable = (pluginName) => {
        const plugin = registeredPlugins.get(pluginName);
        if (plugin === null || plugin === void 0 ? void 0 : plugin.platforms.has(getPlatform())) {
          return true;
        }
        if (getPluginHeader(pluginName)) {
          return true;
        }
        return false;
      };
      const isPluginAvailable = ((_c = capPlatforms === null || capPlatforms === void 0 ? void 0 : capPlatforms.currentPlatform) === null || _c === void 0 ? void 0 : _c.isPluginAvailable) || defaultIsPluginAvailable;
      const defaultGetPluginHeader = (pluginName) => {
        var _a2;
        return (_a2 = cap.PluginHeaders) === null || _a2 === void 0 ? void 0 : _a2.find((h) => h.name === pluginName);
      };
      const getPluginHeader = ((_d = capPlatforms === null || capPlatforms === void 0 ? void 0 : capPlatforms.currentPlatform) === null || _d === void 0 ? void 0 : _d.getPluginHeader) || defaultGetPluginHeader;
      const handleError = (err) => win.console.error(err);
      const pluginMethodNoop = (_target, prop, pluginName) => {
        return Promise.reject(`${pluginName} does not have an implementation of "${prop}".`);
      };
      const registeredPlugins = /* @__PURE__ */ new Map();
      const defaultRegisterPlugin = (pluginName, jsImplementations = {}) => {
        const registeredPlugin = registeredPlugins.get(pluginName);
        if (registeredPlugin) {
          console.warn(`Capacitor plugin "${pluginName}" already registered. Cannot register plugins twice.`);
          return registeredPlugin.proxy;
        }
        const platform = getPlatform();
        const pluginHeader = getPluginHeader(pluginName);
        let jsImplementation;
        const loadPluginImplementation = async () => {
          if (!jsImplementation && platform in jsImplementations) {
            jsImplementation = typeof jsImplementations[platform] === "function" ? jsImplementation = await jsImplementations[platform]() : jsImplementation = jsImplementations[platform];
          } else if (capCustomPlatform !== null && !jsImplementation && "web" in jsImplementations) {
            jsImplementation = typeof jsImplementations["web"] === "function" ? jsImplementation = await jsImplementations["web"]() : jsImplementation = jsImplementations["web"];
          }
          return jsImplementation;
        };
        const createPluginMethod = (impl, prop) => {
          var _a2, _b2;
          if (pluginHeader) {
            const methodHeader = pluginHeader === null || pluginHeader === void 0 ? void 0 : pluginHeader.methods.find((m) => prop === m.name);
            if (methodHeader) {
              if (methodHeader.rtype === "promise") {
                return (options) => cap.nativePromise(pluginName, prop.toString(), options);
              } else {
                return (options, callback) => cap.nativeCallback(pluginName, prop.toString(), options, callback);
              }
            } else if (impl) {
              return (_a2 = impl[prop]) === null || _a2 === void 0 ? void 0 : _a2.bind(impl);
            }
          } else if (impl) {
            return (_b2 = impl[prop]) === null || _b2 === void 0 ? void 0 : _b2.bind(impl);
          } else {
            throw new CapacitorException(`"${pluginName}" plugin is not implemented on ${platform}`, ExceptionCode.Unimplemented);
          }
        };
        const createPluginMethodWrapper = (prop) => {
          let remove;
          const wrapper = (...args) => {
            const p = loadPluginImplementation().then((impl) => {
              const fn = createPluginMethod(impl, prop);
              if (fn) {
                const p2 = fn(...args);
                remove = p2 === null || p2 === void 0 ? void 0 : p2.remove;
                return p2;
              } else {
                throw new CapacitorException(`"${pluginName}.${prop}()" is not implemented on ${platform}`, ExceptionCode.Unimplemented);
              }
            });
            if (prop === "addListener") {
              p.remove = async () => remove();
            }
            return p;
          };
          wrapper.toString = () => `${prop.toString()}() { [capacitor code] }`;
          Object.defineProperty(wrapper, "name", {
            value: prop,
            writable: false,
            configurable: false
          });
          return wrapper;
        };
        const addListener = createPluginMethodWrapper("addListener");
        const removeListener = createPluginMethodWrapper("removeListener");
        const addListenerNative = (eventName, callback) => {
          const call = addListener({ eventName }, callback);
          const remove = async () => {
            const callbackId = await call;
            removeListener({
              eventName,
              callbackId
            }, callback);
          };
          const p = new Promise((resolve) => call.then(() => resolve({ remove })));
          p.remove = async () => {
            console.warn(`Using addListener() without 'await' is deprecated.`);
            await remove();
          };
          return p;
        };
        const proxy = new Proxy({}, {
          get(_, prop) {
            switch (prop) {
              // https://github.com/facebook/react/issues/20030
              case "$$typeof":
                return void 0;
              case "toJSON":
                return () => ({});
              case "addListener":
                return pluginHeader ? addListenerNative : addListener;
              case "removeListener":
                return removeListener;
              default:
                return createPluginMethodWrapper(prop);
            }
          }
        });
        Plugins2[pluginName] = proxy;
        registeredPlugins.set(pluginName, {
          name: pluginName,
          proxy,
          platforms: /* @__PURE__ */ new Set([
            ...Object.keys(jsImplementations),
            ...pluginHeader ? [platform] : []
          ])
        });
        return proxy;
      };
      const registerPlugin2 = ((_e = capPlatforms === null || capPlatforms === void 0 ? void 0 : capPlatforms.currentPlatform) === null || _e === void 0 ? void 0 : _e.registerPlugin) || defaultRegisterPlugin;
      if (!cap.convertFileSrc) {
        cap.convertFileSrc = (filePath) => filePath;
      }
      cap.getPlatform = getPlatform;
      cap.handleError = handleError;
      cap.isNativePlatform = isNativePlatform;
      cap.isPluginAvailable = isPluginAvailable;
      cap.pluginMethodNoop = pluginMethodNoop;
      cap.registerPlugin = registerPlugin2;
      cap.Exception = CapacitorException;
      cap.DEBUG = !!cap.DEBUG;
      cap.isLoggingEnabled = !!cap.isLoggingEnabled;
      cap.platform = cap.getPlatform();
      cap.isNative = cap.isNativePlatform();
      return cap;
    };
    initCapacitorGlobal = (win) => win.Capacitor = createCapacitor(win);
    Capacitor = /* @__PURE__ */ initCapacitorGlobal(typeof globalThis !== "undefined" ? globalThis : typeof self !== "undefined" ? self : typeof window !== "undefined" ? window : typeof global !== "undefined" ? global : {});
    registerPlugin = Capacitor.registerPlugin;
    Plugins = Capacitor.Plugins;
    WebPlugin = class {
      constructor(config) {
        this.listeners = {};
        this.retainedEventArguments = {};
        this.windowListeners = {};
        if (config) {
          console.warn(`Capacitor WebPlugin "${config.name}" config object was deprecated in v3 and will be removed in v4.`);
          this.config = config;
        }
      }
      addListener(eventName, listenerFunc) {
        let firstListener = false;
        const listeners2 = this.listeners[eventName];
        if (!listeners2) {
          this.listeners[eventName] = [];
          firstListener = true;
        }
        this.listeners[eventName].push(listenerFunc);
        const windowListener = this.windowListeners[eventName];
        if (windowListener && !windowListener.registered) {
          this.addWindowListener(windowListener);
        }
        if (firstListener) {
          this.sendRetainedArgumentsForEvent(eventName);
        }
        const remove = async () => this.removeListener(eventName, listenerFunc);
        const p = Promise.resolve({ remove });
        return p;
      }
      async removeAllListeners() {
        this.listeners = {};
        for (const listener in this.windowListeners) {
          this.removeWindowListener(this.windowListeners[listener]);
        }
        this.windowListeners = {};
      }
      notifyListeners(eventName, data, retainUntilConsumed) {
        const listeners2 = this.listeners[eventName];
        if (!listeners2) {
          if (retainUntilConsumed) {
            let args = this.retainedEventArguments[eventName];
            if (!args) {
              args = [];
            }
            args.push(data);
            this.retainedEventArguments[eventName] = args;
          }
          return;
        }
        listeners2.forEach((listener) => listener(data));
      }
      hasListeners(eventName) {
        return !!this.listeners[eventName].length;
      }
      registerWindowListener(windowEventName, pluginEventName) {
        this.windowListeners[pluginEventName] = {
          registered: false,
          windowEventName,
          pluginEventName,
          handler: (event) => {
            this.notifyListeners(pluginEventName, event);
          }
        };
      }
      unimplemented(msg = "not implemented") {
        return new Capacitor.Exception(msg, ExceptionCode.Unimplemented);
      }
      unavailable(msg = "not available") {
        return new Capacitor.Exception(msg, ExceptionCode.Unavailable);
      }
      async removeListener(eventName, listenerFunc) {
        const listeners2 = this.listeners[eventName];
        if (!listeners2) {
          return;
        }
        const index = listeners2.indexOf(listenerFunc);
        this.listeners[eventName].splice(index, 1);
        if (!this.listeners[eventName].length) {
          this.removeWindowListener(this.windowListeners[eventName]);
        }
      }
      addWindowListener(handle) {
        window.addEventListener(handle.windowEventName, handle.handler);
        handle.registered = true;
      }
      removeWindowListener(handle) {
        if (!handle) {
          return;
        }
        window.removeEventListener(handle.windowEventName, handle.handler);
        handle.registered = false;
      }
      sendRetainedArgumentsForEvent(eventName) {
        const args = this.retainedEventArguments[eventName];
        if (!args) {
          return;
        }
        delete this.retainedEventArguments[eventName];
        args.forEach((arg) => {
          this.notifyListeners(eventName, arg);
        });
      }
    };
    encode = (str) => encodeURIComponent(str).replace(/%(2[346B]|5E|60|7C)/g, decodeURIComponent).replace(/[()]/g, escape);
    decode = (str) => str.replace(/(%[\dA-F]{2})+/gi, decodeURIComponent);
    CapacitorCookiesPluginWeb = class extends WebPlugin {
      async getCookies() {
        const cookies = document.cookie;
        const cookieMap = {};
        cookies.split(";").forEach((cookie) => {
          if (cookie.length <= 0)
            return;
          let [key, value] = cookie.replace(/=/, "CAP_COOKIE").split("CAP_COOKIE");
          key = decode(key).trim();
          value = decode(value).trim();
          cookieMap[key] = value;
        });
        return cookieMap;
      }
      async setCookie(options) {
        try {
          const encodedKey = encode(options.key);
          const encodedValue = encode(options.value);
          const expires = `; expires=${(options.expires || "").replace("expires=", "")}`;
          const path = (options.path || "/").replace("path=", "");
          const domain = options.url != null && options.url.length > 0 ? `domain=${options.url}` : "";
          document.cookie = `${encodedKey}=${encodedValue || ""}${expires}; path=${path}; ${domain};`;
        } catch (error) {
          return Promise.reject(error);
        }
      }
      async deleteCookie(options) {
        try {
          document.cookie = `${options.key}=; Max-Age=0`;
        } catch (error) {
          return Promise.reject(error);
        }
      }
      async clearCookies() {
        try {
          const cookies = document.cookie.split(";") || [];
          for (const cookie of cookies) {
            document.cookie = cookie.replace(/^ +/, "").replace(/=.*/, `=;expires=${(/* @__PURE__ */ new Date()).toUTCString()};path=/`);
          }
        } catch (error) {
          return Promise.reject(error);
        }
      }
      async clearAllCookies() {
        try {
          await this.clearCookies();
        } catch (error) {
          return Promise.reject(error);
        }
      }
    };
    CapacitorCookies = registerPlugin("CapacitorCookies", {
      web: () => new CapacitorCookiesPluginWeb()
    });
    readBlobAsBase64 = async (blob) => new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const base64String = reader.result;
        resolve(base64String.indexOf(",") >= 0 ? base64String.split(",")[1] : base64String);
      };
      reader.onerror = (error) => reject(error);
      reader.readAsDataURL(blob);
    });
    normalizeHttpHeaders = (headers = {}) => {
      const originalKeys = Object.keys(headers);
      const loweredKeys = Object.keys(headers).map((k) => k.toLocaleLowerCase());
      const normalized = loweredKeys.reduce((acc, key, index) => {
        acc[key] = headers[originalKeys[index]];
        return acc;
      }, {});
      return normalized;
    };
    buildUrlParams = (params, shouldEncode = true) => {
      if (!params)
        return null;
      const output = Object.entries(params).reduce((accumulator, entry) => {
        const [key, value] = entry;
        let encodedValue;
        let item;
        if (Array.isArray(value)) {
          item = "";
          value.forEach((str) => {
            encodedValue = shouldEncode ? encodeURIComponent(str) : str;
            item += `${key}=${encodedValue}&`;
          });
          item.slice(0, -1);
        } else {
          encodedValue = shouldEncode ? encodeURIComponent(value) : value;
          item = `${key}=${encodedValue}`;
        }
        return `${accumulator}&${item}`;
      }, "");
      return output.substr(1);
    };
    buildRequestInit = (options, extra = {}) => {
      const output = Object.assign({ method: options.method || "GET", headers: options.headers }, extra);
      const headers = normalizeHttpHeaders(options.headers);
      const type = headers["content-type"] || "";
      if (typeof options.data === "string") {
        output.body = options.data;
      } else if (type.includes("application/x-www-form-urlencoded")) {
        const params = new URLSearchParams();
        for (const [key, value] of Object.entries(options.data || {})) {
          params.set(key, value);
        }
        output.body = params.toString();
      } else if (type.includes("multipart/form-data") || options.data instanceof FormData) {
        const form = new FormData();
        if (options.data instanceof FormData) {
          options.data.forEach((value, key) => {
            form.append(key, value);
          });
        } else {
          for (const key of Object.keys(options.data)) {
            form.append(key, options.data[key]);
          }
        }
        output.body = form;
        const headers2 = new Headers(output.headers);
        headers2.delete("content-type");
        output.headers = headers2;
      } else if (type.includes("application/json") || typeof options.data === "object") {
        output.body = JSON.stringify(options.data);
      }
      return output;
    };
    CapacitorHttpPluginWeb = class extends WebPlugin {
      /**
       * Perform an Http request given a set of options
       * @param options Options to build the HTTP request
       */
      async request(options) {
        const requestInit = buildRequestInit(options, options.webFetchExtra);
        const urlParams = buildUrlParams(options.params, options.shouldEncodeUrlParams);
        const url = urlParams ? `${options.url}?${urlParams}` : options.url;
        const response = await fetch(url, requestInit);
        const contentType = response.headers.get("content-type") || "";
        let { responseType = "text" } = response.ok ? options : {};
        if (contentType.includes("application/json")) {
          responseType = "json";
        }
        let data;
        let blob;
        switch (responseType) {
          case "arraybuffer":
          case "blob":
            blob = await response.blob();
            data = await readBlobAsBase64(blob);
            break;
          case "json":
            data = await response.json();
            break;
          case "document":
          case "text":
          default:
            data = await response.text();
        }
        const headers = {};
        response.headers.forEach((value, key) => {
          headers[key] = value;
        });
        return {
          data,
          headers,
          status: response.status,
          url: response.url
        };
      }
      /**
       * Perform an Http GET request given a set of options
       * @param options Options to build the HTTP request
       */
      async get(options) {
        return this.request(Object.assign(Object.assign({}, options), { method: "GET" }));
      }
      /**
       * Perform an Http POST request given a set of options
       * @param options Options to build the HTTP request
       */
      async post(options) {
        return this.request(Object.assign(Object.assign({}, options), { method: "POST" }));
      }
      /**
       * Perform an Http PUT request given a set of options
       * @param options Options to build the HTTP request
       */
      async put(options) {
        return this.request(Object.assign(Object.assign({}, options), { method: "PUT" }));
      }
      /**
       * Perform an Http PATCH request given a set of options
       * @param options Options to build the HTTP request
       */
      async patch(options) {
        return this.request(Object.assign(Object.assign({}, options), { method: "PATCH" }));
      }
      /**
       * Perform an Http DELETE request given a set of options
       * @param options Options to build the HTTP request
       */
      async delete(options) {
        return this.request(Object.assign(Object.assign({}, options), { method: "DELETE" }));
      }
    };
    CapacitorHttp = registerPlugin("CapacitorHttp", {
      web: () => new CapacitorHttpPluginWeb()
    });
  }
});

// ../../mobile-client/node_modules/@capacitor/preferences/dist/esm/web.js
var web_exports = {};
__export(web_exports, {
  PreferencesWeb: () => PreferencesWeb
});
var PreferencesWeb;
var init_web = __esm({
  "../../mobile-client/node_modules/@capacitor/preferences/dist/esm/web.js"() {
    init_dist();
    PreferencesWeb = class extends WebPlugin {
      constructor() {
        super(...arguments);
        this.group = "CapacitorStorage";
      }
      async configure({ group }) {
        if (typeof group === "string") {
          this.group = group;
        }
      }
      async get(options) {
        const value = this.impl.getItem(this.applyPrefix(options.key));
        return { value };
      }
      async set(options) {
        this.impl.setItem(this.applyPrefix(options.key), options.value);
      }
      async remove(options) {
        this.impl.removeItem(this.applyPrefix(options.key));
      }
      async keys() {
        const keys = this.rawKeys().map((k) => k.substring(this.prefix.length));
        return { keys };
      }
      async clear() {
        for (const key of this.rawKeys()) {
          this.impl.removeItem(key);
        }
      }
      async migrate() {
        var _a;
        const migrated = [];
        const existing = [];
        const oldprefix = "_cap_";
        const keys = Object.keys(this.impl).filter((k) => k.indexOf(oldprefix) === 0);
        for (const oldkey of keys) {
          const key = oldkey.substring(oldprefix.length);
          const value = (_a = this.impl.getItem(oldkey)) !== null && _a !== void 0 ? _a : "";
          const { value: currentValue } = await this.get({ key });
          if (typeof currentValue === "string") {
            existing.push(key);
          } else {
            await this.set({ key, value });
            migrated.push(key);
          }
        }
        return { migrated, existing };
      }
      async removeOld() {
        const oldprefix = "_cap_";
        const keys = Object.keys(this.impl).filter((k) => k.indexOf(oldprefix) === 0);
        for (const oldkey of keys) {
          this.impl.removeItem(oldkey);
        }
      }
      get impl() {
        return window.localStorage;
      }
      get prefix() {
        return this.group === "NativeStorage" ? "" : `${this.group}.`;
      }
      rawKeys() {
        return Object.keys(this.impl).filter((k) => k.indexOf(this.prefix) === 0);
      }
      applyPrefix(key) {
        return this.prefix + key;
      }
    };
  }
});

// ../../mobile-client/node_modules/@capacitor/app/dist/esm/web.js
var web_exports2 = {};
__export(web_exports2, {
  AppWeb: () => AppWeb
});
var AppWeb;
var init_web2 = __esm({
  "../../mobile-client/node_modules/@capacitor/app/dist/esm/web.js"() {
    init_dist();
    AppWeb = class extends WebPlugin {
      constructor() {
        super();
        this.handleVisibilityChange = () => {
          const data = {
            isActive: document.hidden !== true
          };
          this.notifyListeners("appStateChange", data);
          if (document.hidden) {
            this.notifyListeners("pause", null);
          } else {
            this.notifyListeners("resume", null);
          }
        };
        document.addEventListener("visibilitychange", this.handleVisibilityChange, false);
      }
      exitApp() {
        throw this.unimplemented("Not implemented on web.");
      }
      async getInfo() {
        throw this.unimplemented("Not implemented on web.");
      }
      async getLaunchUrl() {
        return { url: "" };
      }
      async getState() {
        return { isActive: document.hidden !== true };
      }
      async minimizeApp() {
        throw this.unimplemented("Not implemented on web.");
      }
    };
  }
});

// ../../mobile-client/node_modules/@capacitor/preferences/dist/esm/index.js
init_dist();
var Preferences = registerPlugin("Preferences", {
  web: () => Promise.resolve().then(() => (init_web(), web_exports)).then((m) => new m.PreferencesWeb())
});

// ../../mobile-client/src/core/config.ts
var API_SUFFIX = "/api/v1";
var DEFAULT_API_BASE = "https://al-jehad-production.up.railway.app/api/v1";
var DEFAULT_DEV_BASE = "http://10.0.2.2:8000/api/v1";
function normalizeBaseUrl(raw2) {
  let url = raw2.trim().replace(/\/+$/, "");
  if (!url) return "";
  if (!/^https?:\/\//i.test(url)) {
    url = `https://${url}`;
  }
  if (!/\/api\/v1\/?$/i.test(url)) {
    url = `${url}${API_SUFFIX}`;
  }
  return url.replace(/\/+$/, "");
}
function viteBaseUrl() {
  const env = import.meta.env;
  const raw2 = typeof env?.VITE_API_BASE_URL === "string" ? env.VITE_API_BASE_URL : "";
  if (raw2 && raw2.trim()) return normalizeBaseUrl(raw2);
  if (env?.DEV) return DEFAULT_DEV_BASE;
  return "";
}
var cachedResolved = null;
async function resolveApiBaseUrl() {
  if (cachedResolved) return cachedResolved;
  cachedResolved = viteBaseUrl() || DEFAULT_API_BASE;
  return cachedResolved;
}

// ../../mobile-client/src/core/locale.ts
var LOCALES = [
  { code: "ar", name: "\u0627\u0644\u0639\u0631\u0628\u064A\u0629", dir: "rtl" },
  { code: "en", name: "English", dir: "ltr" },
  { code: "hi", name: "\u0939\u093F\u0928\u094D\u0926\u0940", dir: "ltr" },
  { code: "ur", name: "\u0627\u0631\u062F\u0648", dir: "rtl" }
];
var DEFAULT_LOCALE = "en";
var current = DEFAULT_LOCALE;
function getLocale() {
  return current;
}
function acceptLanguage() {
  return current;
}

// ../../mobile-client/src/core/store.ts
function deepEqual(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}
var Store = class {
  state = {
    session: null,
    shift: null,
    geofence: null,
    offlineQueueDepth: 0,
    lastSyncAt: null,
    initialized: false
  };
  listeners = /* @__PURE__ */ new Set();
  getState() {
    return this.state;
  }
  subscribe(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  emit() {
    const snapshot = this.state;
    for (const fn of this.listeners) {
      try {
        fn(snapshot);
      } catch {
      }
    }
  }
  setSession(session) {
    if (deepEqual(this.state.session, session)) return;
    this.state = { ...this.state, session };
    this.emit();
  }
  setShift(shift) {
    if (deepEqual(this.state.shift, shift)) return;
    this.state = { ...this.state, shift };
    this.emit();
  }
  setGeofence(geofence) {
    if (deepEqual(this.state.geofence, geofence)) return;
    this.state = { ...this.state, geofence };
    this.emit();
  }
  setOfflineQueueDepth(depth) {
    const n = Math.max(0, Math.floor(depth));
    if (this.state.offlineQueueDepth === n) return;
    this.state = { ...this.state, offlineQueueDepth: n };
    this.emit();
  }
  setLastSyncAt(iso) {
    if (this.state.lastSyncAt === iso) return;
    this.state = { ...this.state, lastSyncAt: iso };
    this.emit();
  }
  setInitialized(v) {
    if (this.state.initialized === v) return;
    this.state = { ...this.state, initialized: v };
    this.emit();
  }
  patch(partial) {
    let changed = false;
    const next = { ...this.state };
    for (const k of Object.keys(partial)) {
      const nv = partial[k];
      const cv = this.state[k];
      if (!deepEqual(cv, nv)) {
        next[k] = nv;
        changed = true;
      }
    }
    if (!changed) return;
    this.state = next;
    this.emit();
  }
  select(selector) {
    return selector(this.state);
  }
};
var store = new Store();

// ../../mobile-client/src/core/http.ts
var TOKEN_KEY = "auth_token";
var TOKEN_EXPIRES_KEY = "auth_expires_at";
var USER_KEY = "auth_user";
var TOKEN_VERSION_KEY = "auth_token_version";
function makeApiError(message, status, code, offline, detail) {
  const e = new Error(message);
  e.status = status;
  e.code = code;
  e.offline = offline;
  e.detail = detail;
  return e;
}
function buildUrl(base, path, query) {
  const p = path.startsWith("/") ? path : `/${path}`;
  const url = new URL(`${base}${p}`);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v === null || v === void 0 || v === "") continue;
      url.searchParams.set(k, String(v));
    }
  }
  return url.toString();
}
async function loadToken() {
  try {
    const { value } = await Preferences.get({ key: TOKEN_KEY });
    return value ?? null;
  } catch {
    return store.getState().session?.token ?? null;
  }
}
async function saveSession(token, expiresAt, userJson) {
  await Preferences.set({ key: TOKEN_KEY, value: token });
  if (expiresAt) await Preferences.set({ key: TOKEN_EXPIRES_KEY, value: expiresAt });
  else await Preferences.remove({ key: TOKEN_EXPIRES_KEY });
  if (userJson) await Preferences.set({ key: USER_KEY, value: userJson });
}
async function persistSessionFromLoginResponse(body) {
  const token = body["access_token"] ?? body["token"] ?? "";
  if (!token) throw new Error("Login response missing access_token");
  const expiresAt = body["expires_at"] ?? body["expiresAt"] ?? null;
  const user = body["user"];
  const userJson = user ? JSON.stringify(user) : null;
  const ver = body["token_version"] ?? body["ver"] ?? 0;
  await saveSession(token, expiresAt, userJson);
  await Preferences.set({ key: TOKEN_VERSION_KEY, value: String(ver) });
  if (user) {
    store.setSession({
      user: {
        id: String(user["id"] ?? ""),
        name: String(user["name"] ?? ""),
        role: String(user["role"] ?? "worker"),
        email: user["email"] ?? null,
        phone: user["phone"] ?? null
      },
      token,
      expiresAt,
      tokenVersion: Number(ver) || 0
    });
  }
}
async function clearPersistedSession() {
  await Preferences.remove({ key: TOKEN_KEY });
  await Preferences.remove({ key: TOKEN_EXPIRES_KEY });
  await Preferences.remove({ key: USER_KEY });
  await Preferences.remove({ key: TOKEN_VERSION_KEY });
  store.setSession(null);
}
var refreshPromise = null;
async function refreshToken() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const token = await loadToken();
    if (!token) return null;
    const base = await resolveApiBaseUrl();
    const url = `${base}/auth/refresh`;
    const controller = new AbortController();
    const t2 = setTimeout(() => controller.abort(), 1e4);
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: {
          Accept: "application/json",
          // The worker's language, on the refresh as on every other call: a refusal
          // refreshes a session the same person is about to read.
          "Accept-Language": acceptLanguage(),
          Authorization: `Bearer ${token}`
        },
        signal: controller.signal
      });
      if (!res.ok) {
        await clearPersistedSession();
        return null;
      }
      const body = await res.json().catch(() => null);
      if (!body) return null;
      await persistSessionFromLoginResponse(body);
      const { value } = await Preferences.get({ key: TOKEN_KEY });
      return value ?? null;
    } catch {
      return null;
    } finally {
      clearTimeout(t2);
    }
  })();
  try {
    return await refreshPromise;
  } finally {
    refreshPromise = null;
  }
}
function isNetworkFailure(err) {
  if (err instanceof DOMException && err.name === "AbortError") return false;
  if (err instanceof TypeError) return true;
  const msg = String(err?.message ?? err ?? "").toLowerCase();
  return msg.includes("networkerror") || msg.includes("failed to fetch") || msg.includes("load failed");
}
var DEFAULT_TIMEOUT_MS = 15e3;
var RAILWAY_COLD_START_RETRY_MS = 1200;
function isRailwayColdStartStatus(status) {
  return status === 502 || status === 503 || status === 504;
}
function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
async function doFetchWithTimeout(url, init, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}
async function request(opts) {
  const base = await resolveApiBaseUrl();
  const url = buildUrl(base, opts.path, opts.query);
  const needsAuth = opts.auth !== false;
  let token = null;
  if (needsAuth) token = await loadToken();
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const headers = {
    Accept: "application/json",
    // The language the screen is drawn in. Safe to send unconditionally: it is a
    // CORS-safelisted header, so it adds no preflight, and a server that does not
    // localize anything reads past it.
    "Accept-Language": acceptLanguage(),
    ...opts.headers ?? {}
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  let body;
  if (opts.formData) {
    body = opts.formData;
    delete headers["Content-Type"];
  } else if (opts.body !== void 0) {
    headers["Content-Type"] = headers["Content-Type"] ?? "application/json";
    body = JSON.stringify(opts.body);
  }
  let res;
  try {
    res = await doFetchWithTimeout(url, { method: opts.method ?? "GET", headers, body }, timeoutMs);
  } catch (err) {
    if (isNetworkFailure(err)) {
      throw makeApiError("Network unavailable", 0, "offline", true);
    }
    if (err instanceof DOMException && err.name === "AbortError") {
      throw makeApiError("Request timed out", 0, "timeout");
    }
    throw makeApiError(String(err.message ?? "Network error"), 0, "network_error", true);
  }
  if (isRailwayColdStartStatus(res.status) && !opts._coldStartRetried) {
    await sleep(RAILWAY_COLD_START_RETRY_MS);
    return request({ ...opts, _coldStartRetried: true });
  }
  if (res.status === 401 && needsAuth && opts.retryOn401 !== false) {
    const next = await refreshToken();
    if (next) {
      return request({ ...opts, retryOn401: false });
    }
    throw makeApiError("Not authenticated", 401, "unauthorized");
  }
  if (!res.ok) {
    let detail = null;
    let code;
    let structured;
    try {
      const j = await res.json();
      detail = j["detail"] ?? j;
      if (typeof detail === "object" && detail !== null) {
        structured = detail;
        code = structured["error_code"];
        if (!code) code = j["error_code"] ?? j["code"] ?? void 0;
      } else if (typeof detail === "string") {
        code = detail;
      }
    } catch {
    }
    const msg = typeof detail === "string" ? detail : structured?.["message"] ?? structured?.["error_code"] ?? `Request failed (${res.status})`;
    throw makeApiError(msg, res.status, code, false, structured);
  }
  if (res.status === 204) return void 0;
  const text = await res.text();
  if (!text) return void 0;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

// ../../mobile-client/src/offline/signing.ts
var SIGNATURE_VERSION = 1;
function bytesToHex(bytes) {
  let hex = "";
  for (let i = 0; i < bytes.length; i++) hex += bytes[i].toString(16).padStart(2, "0");
  return hex;
}
function formatTs(epochMs) {
  const d = new Date(epochMs);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}
function monotonicNow() {
  if (typeof performance !== "undefined" && typeof performance.now === "function" && typeof performance.timeOrigin === "number") {
    return performance.timeOrigin + performance.now();
  }
  return Date.now();
}
function randomHex(bytes) {
  const buf = new Uint8Array(new ArrayBuffer(bytes));
  crypto.getRandomValues(buf);
  return bytesToHex(buf);
}

// ../../mobile-client/src/offline/db.ts
var OFFLINE_DB_NAME = "site_attendance_offline";
var OFFLINE_DB_VERSION = 1;
function idbRequest(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("IndexedDB request failed"));
  });
}
var opening = null;
function openDb() {
  if (opening) return opening;
  opening = new Promise((resolve, reject) => {
    const req = indexedDB.open(OFFLINE_DB_NAME, OFFLINE_DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("meta")) {
        db.createObjectStore("meta", { keyPath: "key" });
      }
      if (!db.objectStoreNames.contains("punches")) {
        const s = db.createObjectStore("punches", { keyPath: "client_punch_id" });
        s.createIndex("by_worker_status", ["worker_id", "status"]);
        s.createIndex("by_worker", "worker_id");
      }
      if (!db.objectStoreNames.contains("photos")) {
        db.createObjectStore("photos", { keyPath: "client_punch_id" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error ?? new Error("IndexedDB unavailable"));
  });
  opening.catch(() => {
    opening = null;
  });
  return opening;
}
async function dbGet(store2, key) {
  const db = await openDb();
  return idbRequest(db.transaction(store2, "readonly").objectStore(store2).get(key));
}
async function dbPut(store2, value) {
  const db = await openDb();
  await idbRequest(db.transaction(store2, "readwrite").objectStore(store2).put(value));
}
async function dbDelete(store2, key) {
  const db = await openDb();
  await idbRequest(db.transaction(store2, "readwrite").objectStore(store2).delete(key));
}
async function dbGetAll(store2) {
  const db = await openDb();
  return idbRequest(db.transaction(store2, "readonly").objectStore(store2).getAll());
}
async function dbGetAllByIndex(store2, indexName, range) {
  const db = await openDb();
  const idx = db.transaction(store2, "readonly").objectStore(store2).index(indexName);
  if (range !== void 0) {
    return idbRequest(idx.getAll(range));
  }
  return idbRequest(idx.getAll());
}

// ../../mobile-client/src/offline/queue.ts
var OfflineError = class extends Error {
  code;
  constructor(code, message) {
    super(message ?? code);
    this.name = "OfflineError";
    this.code = code;
  }
};
function metaKey(kind, workerId) {
  return `${kind}:${workerId}`;
}
async function getMeta(key) {
  const rec = await dbGet("meta", key);
  return rec ? rec.value : null;
}
async function setMeta(key, value) {
  await dbPut("meta", { key, value });
}
async function delMeta(key) {
  await dbDelete("meta", key);
}
async function loadDevice(workerId) {
  return getMeta(metaKey("device", workerId));
}
async function saveDevice(workerId, rec) {
  await setMeta(metaKey("device", workerId), rec);
}
async function loadAnchor(workerId) {
  return getMeta(metaKey("anchor", workerId));
}
async function saveAnchor(workerId, rec) {
  await setMeta(metaKey("anchor", workerId), rec);
}
async function clearAnchor(workerId) {
  await delMeta(metaKey("anchor", workerId));
}
async function clearDevice(workerId) {
  await delMeta(metaKey("device", workerId));
}
function anchorAgeSeconds(anchor) {
  return Math.max(0, (monotonicNow() - anchor.mono_ms) / 1e3);
}
async function queuedPunches(workerId) {
  const rows = await dbGetAllByIndex(
    "punches",
    "by_worker_status",
    IDBKeyRange.only([String(workerId), "queued"])
  );
  return rows.sort((a, b) => (a.captured_mono_ms || 0) - (b.captured_mono_ms || 0));
}
async function pendingCount(workerId) {
  return (await queuedPunches(workerId)).length;
}
function toSyncPayload(p) {
  return {
    client_punch_id: p.client_punch_id,
    action: p.action,
    anchor_id: p.anchor_id,
    anchor_server_time: p.anchor_server_time,
    anchor_signature: p.anchor_signature,
    monotonic_offset_s: p.monotonic_offset_s,
    nonce: p.nonce,
    lat: p.lat,
    lon: p.lon,
    accuracy: p.accuracy,
    client_timestamp: p.client_timestamp,
    client_offset_s: p.client_offset_s,
    photo_sha256: p.photo_sha256,
    signature: p.signature,
    signature_version: p.signature_version
  };
}

// ../../mobile-client/src/offline/device.ts
var DEVICE_KEY_PREFIX = "offline_device_key:";
function newDeviceId(workerId) {
  return `android-${workerId}-${randomHex(8)}`.slice(0, 120);
}
async function stashKey(workerId, key) {
  await Preferences.set({ key: `${DEVICE_KEY_PREFIX}${workerId}`, value: key });
}
async function readStashedKey(workerId) {
  try {
    const { value } = await Preferences.get({ key: `${DEVICE_KEY_PREFIX}${workerId}` });
    return value ?? null;
  } catch {
    return null;
  }
}
async function ensureDevice(workerId) {
  const id = String(workerId);
  if (!id) throw new OfflineError("no_session", "Sign in before registering a device.");
  const stored = await loadDevice(id);
  if (stored?.device_key && stored.worker_id === id) return stored;
  const stashed = await readStashedKey(id);
  if (stashed && stored?.device_id) {
    const restored = { ...stored, device_key: stashed };
    await saveDevice(id, restored);
    return restored;
  }
  const deviceId = stored?.device_id ?? newDeviceId(id);
  return registerDevice(id, deviceId);
}
async function registerDevice(workerId, deviceId) {
  const id = String(workerId);
  let body;
  try {
    body = await request({
      method: "POST",
      path: "/attendance/devices/register",
      body: { device_id: deviceId, note: "android client (offline queue)" }
    });
  } catch (e) {
    const err = e;
    if (err.status === 409) {
      throw new OfflineError(
        "device_key_lost",
        "This device is already registered but its signing key is gone. Sync everything waiting on it, then re-register the device."
      );
    }
    if (err.status === 401) {
      throw new OfflineError("unauthorized", "Session expired. Sign in again.");
    }
    if (err.offline) {
      throw new OfflineError("offline", "No connection: a device cannot be registered offline.");
    }
    throw new OfflineError(err.code ?? "register_failed", err.message ?? "Device registration failed.");
  }
  if (!body?.device_key) {
    throw new OfflineError("register_failed", "The server did not return a signing key.");
  }
  const record = {
    worker_id: id,
    device_id: body.device_id || deviceId,
    device_key: body.device_key,
    key_epoch: Number(body.key_epoch ?? 0),
    registered_at: formatTs(Date.now()),
    origin: "android"
  };
  await stashKey(id, record.device_key);
  await saveDevice(id, record);
  return record;
}
async function rotateDevice(workerId) {
  const id = String(workerId);
  const pending = await queuedPunches(id);
  if (pending.length) {
    throw new OfflineError(
      "queue_not_empty",
      `${pending.length} punch(es) on this device have not synced yet. Sync them before re-registering, or they will lose their signatures.`
    );
  }
  const stored = await loadDevice(id);
  if (stored?.device_id) {
    try {
      await request({
        method: "POST",
        path: `/attendance/devices/${encodeURIComponent(stored.device_id)}/revoke`,
        body: {}
      });
    } catch (e) {
      const err = e;
      if (err.offline) {
        throw new OfflineError("offline", "No connection: the device cannot be revoked offline.");
      }
      if (err.status !== 404) {
        throw new OfflineError("revoke_failed", err.message ?? "The device could not be revoked.");
      }
    }
  }
  await clearDevice(id);
  await clearAnchor(id);
  await Preferences.remove({ key: `${DEVICE_KEY_PREFIX}${id}` });
  return ensureDevice(id);
}
async function adoptAnchor(workerId, next, deviceId) {
  const id = String(workerId);
  const previous = await loadAnchor(id);
  const anchor = {
    worker_id: id,
    device_id: deviceId,
    anchor_id: next.anchor_id,
    server_time: next.server_time,
    anchor_signature: next.anchor_signature,
    signature_version: next.signature_version ?? SIGNATURE_VERSION,
    max_offline_hours: previous?.max_offline_hours || 72,
    mono_ms: monotonicNow(),
    wall_ms: Date.now(),
    fetched_at: formatTs(Date.now())
  };
  await saveAnchor(id, anchor);
  return anchor;
}
async function anchorState(workerId) {
  const anchor = await loadAnchor(String(workerId));
  if (!anchor) return { anchor: null, fresh: false, age_seconds: 0, expires_in_seconds: 0 };
  const age = anchorAgeSeconds(anchor);
  const max = Number(anchor.max_offline_hours || 72) * 3600;
  return {
    anchor,
    fresh: age <= max,
    age_seconds: Math.round(age),
    expires_in_seconds: Math.max(0, Math.round(max - age))
  };
}

// ../../mobile-client/src/offline/sync.ts
var OFFLINE_BATCH_CHUNK = 25;
var OFFLINE_HISTORY_RETENTION_DAYS = 7;
var OFFLINE_PHOTO_RETENTION_HOURS = 72;
function localStatus(serverStatus) {
  if (serverStatus === "accepted" || serverStatus === "flagged") return "synced";
  if (serverStatus === "duplicate") return "duplicate";
  if (serverStatus === "rejected") return "rejected";
  return "queued";
}
async function uploadPhoto(punch, blob) {
  const form = new FormData();
  form.append("client_punch_id", punch.client_punch_id);
  const name = String(blob.type || "").includes("png") ? "selfie.png" : "selfie.jpg";
  form.append("photo", blob, name);
  try {
    await request({ method: "POST", path: "/attendance/sync/photo", formData: form });
    return { ok: true };
  } catch (e) {
    const err = e;
    if (err.offline) return { ok: false, offline: true, message: err.message };
    return { ok: false, message: err.message ?? "photo upload failed" };
  }
}
async function loadPhotoBlob(clientPunchId) {
  const rec = await dbGet("photos", clientPunchId).catch(
    () => null
  );
  return rec ? rec.blob : null;
}
async function prune(workerId) {
  const punches = await dbGetAllByIndex("punches", "by_worker", String(workerId));
  const historyCutoff = Date.now() - OFFLINE_HISTORY_RETENTION_DAYS * 864e5;
  const photoCutoff = Date.now() - OFFLINE_PHOTO_RETENTION_HOURS * 36e5;
  const keepPhoto = /* @__PURE__ */ new Set();
  for (const p of punches) {
    if (p.status === "queued") {
      keepPhoto.add(p.client_punch_id);
      continue;
    }
    if ((p.captured_wall_ms || 0) < historyCutoff) {
      await dbDelete("punches", p.client_punch_id);
      continue;
    }
    if (!p.photo_uploaded_at && (p.captured_wall_ms || 0) >= photoCutoff) {
      keepPhoto.add(p.client_punch_id);
    }
  }
  const photos = await dbGetAll("photos").catch(() => []);
  for (const ph of photos) {
    if (!keepPhoto.has(ph.client_punch_id)) await dbDelete("photos", ph.client_punch_id);
  }
}
async function syncNow(workerId, deviceId) {
  const summary = {
    sent: 0,
    applied: 0,
    flagged: 0,
    rejected: 0,
    duplicates: 0,
    pending: 0,
    offline: false,
    error: null,
    results: [],
    photos: { uploaded: 0, pending: 0, error: null }
  };
  if (!workerId || !deviceId) {
    summary.error = "no_session";
    return summary;
  }
  summary.pending = await queuedPunches(workerId).then((r) => r.length).catch(() => 0);
  let chunkSize = OFFLINE_BATCH_CHUNK;
  let queue = await queuedPunches(workerId);
  let rounds = 0;
  while (queue.length && rounds < 20 && !summary.offline) {
    rounds += 1;
    const batch = queue.slice(0, chunkSize);
    let body;
    try {
      body = await request({
        method: "POST",
        path: "/attendance/sync",
        body: { device_id: deviceId, punches: batch.map(toSyncPayload) }
      });
    } catch (e) {
      const err = e;
      if (err.offline) {
        summary.offline = true;
        summary.error = "offline";
        break;
      }
      if (err.status === 413 && chunkSize > 1) {
        chunkSize = Math.max(1, Math.floor(chunkSize / 2));
        continue;
      }
      if (err.status === 401) {
        summary.error = "unauthorized";
        summary.message = "Session expired \u2014 sign in again.";
        break;
      }
      if (err.status === 403 && err.code === "device_revoked") {
        summary.error = "device_revoked";
        summary.message = "This device was revoked.";
        break;
      }
      if (err.status === 404) {
        summary.error = err.code ?? "device_unknown";
        summary.message = err.message ?? "Device unknown \u2014 re-register.";
        break;
      }
      summary.error = "sync_failed";
      summary.message = err.message ?? "Sync failed";
      break;
    }
    const verdicts = new Map((body.results ?? []).map((r) => [r.client_punch_id, r]));
    summary.sent += batch.length;
    summary.applied += Number(body.applied) || 0;
    for (const rec of batch) {
      const v = verdicts.get(rec.client_punch_id) ?? {
        client_punch_id: rec.client_punch_id,
        status: "rejected",
        code: "no_verdict"
      };
      const status = localStatus(v.status);
      const settled = {
        ...rec,
        status,
        server_status: v.status,
        rejection_code: v.code ?? null,
        effective_time: v.effective_time ?? rec.effective_timestamp,
        site: v.site ?? null,
        flushed_at: formatTs(Date.now())
      };
      await dbPut("punches", settled);
      if (status === "rejected") summary.rejected += 1;
      else if (status === "duplicate") summary.duplicates += 1;
      else if (v.flagged) summary.flagged += 1;
      summary.results.push({
        client_punch_id: rec.client_punch_id,
        action: rec.action,
        status: v.status,
        code: v.code ?? null,
        effective_time: v.effective_time ?? null
      });
      if (status === "synced" && rec.photo_sha256) {
        const blob = await loadPhotoBlob(rec.client_punch_id);
        if (blob) {
          const up = await uploadPhoto(rec, blob);
          if (up.ok) {
            summary.photos.uploaded += 1;
            await dbDelete("photos", rec.client_punch_id).catch(() => {
            });
            await dbPut("punches", { ...settled, photo_uploaded_at: formatTs(Date.now()) });
          } else {
            summary.photos.pending += 1;
            summary.photos.error = up.message ?? "photo_upload_failed";
            if (up.offline) {
              summary.offline = true;
              break;
            }
          }
        }
      }
    }
    if (body.next_anchor) {
      await adoptAnchor(
        workerId,
        {
          anchor_id: body.next_anchor.anchor_id,
          server_time: body.next_anchor.server_time,
          anchor_signature: body.next_anchor.anchor_signature,
          signature_version: body.next_anchor.signature_version
        },
        deviceId
      );
    }
    const remaining = await queuedPunches(workerId);
    const stillPending = new Set(remaining.map((r) => r.client_punch_id));
    const progressed = batch.some((b) => !stillPending.has(b.client_punch_id));
    if (!progressed) {
      summary.error = "no_progress";
      summary.message = "The server accepted the batch but settled none of its punches; stopping instead of retrying in a loop.";
      break;
    }
    queue = remaining;
  }
  summary.pending = await queuedPunches(workerId).then((r) => r.length).catch(() => summary.pending);
  summary.at = formatTs(Date.now());
  store.setOfflineQueueDepth(summary.pending);
  store.setLastSyncAt(summary.at);
  await prune(workerId).catch(() => {
  });
  return summary;
}

// ../../mobile-client/src/core/router.ts
init_dist();

// ../../mobile-client/node_modules/@capacitor/app/dist/esm/index.js
init_dist();
var App = registerPlugin("App", {
  web: () => Promise.resolve().then(() => (init_web2(), web_exports2)).then((m) => new m.AppWeb())
});

// ../../mobile-client/src/core/router.ts
var TAB_IDS = ["clock", "history", "alerts", "notes", "profile"];
function parseHash(hash) {
  const raw2 = hash.replace(/^#\/?/, "").trim();
  if (!raw2) return { tab: "clock", path: "/", params: {} };
  const [segment, qs] = raw2.split("?");
  const tab = segment.split("/")[0] || "clock";
  const safeTab = TAB_IDS.includes(tab) ? tab : "clock";
  const params = {};
  if (qs) {
    for (const part of qs.split("&")) {
      const [k, v] = part.split("=");
      if (k) params[decodeURIComponent(k)] = decodeURIComponent(v ?? "");
    }
  }
  return { tab: safeTab, path: `/${segment}`, params };
}
function toHash(tab, params) {
  if (!params || Object.keys(params).length === 0) return `#/${tab}`;
  const qs = Object.entries(params).map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");
  return `#/${tab}?${qs}`;
}
var Router = class {
  listeners = /* @__PURE__ */ new Set();
  current = parseHash(typeof window !== "undefined" ? window.location.hash : "");
  history = [this.current.tab];
  backHandler = null;
  started = false;
  getRoute() {
    return this.current;
  }
  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }
  emit() {
    for (const fn of this.listeners) {
      try {
        fn(this.current);
      } catch {
      }
    }
  }
  navigate(tab, params, opts = {}) {
    const hash = toHash(tab, params);
    if (opts.replace) {
      window.location.replace(hash);
    } else {
      window.location.hash = hash;
    }
    this.current = parseHash(hash);
    if (this.history[this.history.length - 1] !== tab) this.history.push(tab);
    if (this.history.length > 20) this.history.shift();
    this.emit();
  }
  start() {
    if (this.started) return;
    this.started = true;
    this.current = parseHash(window.location.hash);
    if (!window.location.hash) {
      window.location.hash = `#/${this.current.tab}`;
    }
    window.addEventListener("hashchange", () => {
      const next = parseHash(window.location.hash);
      if (this.current.tab !== next.tab) {
        if (this.history[this.history.length - 1] !== next.tab) this.history.push(next.tab);
        if (this.history.length > 20) this.history.shift();
      }
      this.current = next;
      this.emit();
    });
    if (Capacitor.isNativePlatform()) {
      App.addListener("backButton", ({ canGoBack }) => {
        if (this.history.length > 1) {
          this.history.pop();
          const prev = this.history[this.history.length - 1] ?? "clock";
          window.location.hash = `#/${prev}`;
          return;
        }
        if (canGoBack) {
          window.history.back();
          return;
        }
        App.exitApp();
      }).then((h) => {
        this.backHandler = h;
      });
    }
  }
  stop() {
    if (this.backHandler) {
      this.backHandler.remove();
      this.backHandler = null;
    }
    this.started = false;
  }
  __test__getHistory() {
    return [...this.history];
  }
};
var router = new Router();

// ../../mobile-client/src/core/strings.ts
var EN = {
  "app.name": "Site attendance",
  "header.roleSignedOut": "Sign in",
  "nav.sections": "Sections",
  "nav.clock": "Clock",
  "nav.history": "History",
  "nav.alerts": "Alerts",
  "nav.notes": "Notes",
  "nav.profile": "Profile",
  "theme.label": "Theme",
  "theme.switchToDark": "Switch to dark screen",
  "theme.switchToLight": "Switch to light screen",
  "language.label": "Language",
  "auth.subtitle": "Sign in with the ID and details your administrator gave you.",
  "auth.userId": "Worker ID",
  "auth.emailOrPhone": "Email or phone",
  "auth.password": "Password",
  "auth.signIn": "Sign in",
  "auth.signingIn": "Signing in\u2026",
  "auth.welcome": "Welcome, {name}",
  "auth.pendingApproval": "Signed in. Your account is waiting for approval before you can record attendance.",
  "auth.offline": "No connection to the server. Check your signal and try again.",
  "auth.rejected": "That ID, email or password was not accepted.",
  "auth.forbidden": "This account cannot sign in.",
  "auth.tooMany": "Too many attempts. Wait a minute and try again.",
  "auth.failed": "Sign-in failed."
};
var AR = {
  "app.name": "\u0646\u0638\u0627\u0645 \u062D\u0636\u0648\u0631 \u0627\u0644\u0645\u0648\u0642\u0639",
  "header.roleSignedOut": "\u062A\u0633\u062C\u064A\u0644 \u0627\u0644\u062F\u062E\u0648\u0644",
  "nav.sections": "\u0627\u0644\u0623\u0642\u0633\u0627\u0645",
  "nav.clock": "\u0627\u0644\u062D\u0636\u0648\u0631",
  "nav.history": "\u0627\u0644\u0633\u062C\u0644",
  "nav.alerts": "\u0627\u0644\u062A\u0646\u0628\u064A\u0647\u0627\u062A",
  "nav.notes": "\u0627\u0644\u0645\u0644\u0627\u062D\u0638\u0627\u062A",
  "nav.profile": "\u0627\u0644\u062D\u0633\u0627\u0628",
  "theme.label": "\u0627\u0644\u0633\u0645\u0629",
  "theme.switchToDark": "\u0627\u0644\u062A\u0628\u062F\u064A\u0644 \u0625\u0644\u0649 \u0627\u0644\u0634\u0627\u0634\u0629 \u0627\u0644\u062F\u0627\u0643\u0646\u0629",
  "theme.switchToLight": "\u0627\u0644\u062A\u0628\u062F\u064A\u0644 \u0625\u0644\u0649 \u0627\u0644\u0634\u0627\u0634\u0629 \u0627\u0644\u0641\u0627\u062A\u062D\u0629",
  "language.label": "\u0627\u0644\u0644\u063A\u0629",
  "auth.subtitle": "\u0633\u062C\u0651\u0644 \u0627\u0644\u062F\u062E\u0648\u0644 \u0628\u0631\u0642\u0645 \u0627\u0644\u0645\u0633\u062A\u062E\u062F\u0645 \u0648\u0627\u0644\u0628\u064A\u0627\u0646\u0627\u062A \u0627\u0644\u062A\u064A \u0623\u0639\u0637\u0627\u0643 \u0625\u064A\u0627\u0647\u0627 \u0627\u0644\u0645\u062F\u064A\u0631.",
  "auth.userId": "\u0631\u0642\u0645 \u0627\u0644\u0645\u0633\u062A\u062E\u062F\u0645",
  "auth.emailOrPhone": "\u0627\u0644\u0628\u0631\u064A\u062F \u0627\u0644\u0625\u0644\u0643\u062A\u0631\u0648\u0646\u064A \u0623\u0648 \u0627\u0644\u0647\u0627\u062A\u0641",
  "auth.password": "\u0643\u0644\u0645\u0629 \u0627\u0644\u0645\u0631\u0648\u0631",
  "auth.signIn": "\u062A\u0633\u062C\u064A\u0644 \u0627\u0644\u062F\u062E\u0648\u0644",
  "auth.signingIn": "\u062C\u0627\u0631\u064D \u062A\u0633\u062C\u064A\u0644 \u0627\u0644\u062F\u062E\u0648\u0644\u2026",
  "auth.welcome": "\u0645\u0631\u062D\u0628\u0627\u064B\u060C {name}",
  "auth.pendingApproval": "\u062A\u0645 \u062A\u0633\u062C\u064A\u0644 \u0627\u0644\u062F\u062E\u0648\u0644. \u062D\u0633\u0627\u0628\u0643 \u0628\u0627\u0646\u062A\u0638\u0627\u0631 \u0627\u0644\u0645\u0648\u0627\u0641\u0642\u0629 \u0642\u0628\u0644 \u062A\u0633\u062C\u064A\u0644 \u0627\u0644\u062D\u0636\u0648\u0631.",
  "auth.offline": "\u0644\u0627 \u0627\u062A\u0635\u0627\u0644 \u0628\u0627\u0644\u062E\u0627\u062F\u0645. \u062A\u062D\u0642\u0642 \u0645\u0646 \u0627\u0644\u0634\u0628\u0643\u0629 \u0648\u062D\u0627\u0648\u0644 \u0645\u0631\u0629 \u0623\u062E\u0631\u0649.",
  "auth.rejected": "\u0644\u0645 \u064A\u064F\u0642\u0628\u0644 \u0631\u0642\u0645 \u0627\u0644\u0645\u0633\u062A\u062E\u062F\u0645 \u0623\u0648 \u0627\u0644\u0628\u0631\u064A\u062F \u0627\u0644\u0625\u0644\u0643\u062A\u0631\u0648\u0646\u064A \u0623\u0648 \u0643\u0644\u0645\u0629 \u0627\u0644\u0645\u0631\u0648\u0631.",
  "auth.forbidden": "\u0647\u0630\u0627 \u0627\u0644\u062D\u0633\u0627\u0628 \u0644\u0627 \u064A\u0645\u0643\u0646\u0647 \u062A\u0633\u062C\u064A\u0644 \u0627\u0644\u062F\u062E\u0648\u0644.",
  "auth.tooMany": "\u0645\u062D\u0627\u0648\u0644\u0627\u062A \u0643\u062B\u064A\u0631\u0629. \u0627\u0646\u062A\u0638\u0631 \u062F\u0642\u064A\u0642\u0629 \u062B\u0645 \u062D\u0627\u0648\u0644 \u0645\u0631\u0629 \u0623\u062E\u0631\u0649.",
  "auth.failed": "\u062A\u0639\u0630\u0651\u0631 \u062A\u0633\u062C\u064A\u0644 \u0627\u0644\u062F\u062E\u0648\u0644."
};
var HI = {
  "app.name": "\u0938\u093E\u0907\u091F \u0909\u092A\u0938\u094D\u0925\u093F\u0924\u093F",
  "header.roleSignedOut": "\u0932\u0949\u0917 \u0907\u0928 \u0915\u0930\u0947\u0902",
  "nav.sections": "\u0905\u0928\u0941\u092D\u093E\u0917",
  "nav.clock": "\u0905\u091F\u0947\u0902\u0921\u0947\u0902\u0938",
  "nav.history": "\u0907\u0924\u093F\u0939\u093E\u0938",
  "nav.alerts": "\u0905\u0932\u0930\u094D\u091F",
  "nav.notes": "\u0928\u094B\u091F\u094D\u0938",
  "nav.profile": "\u092A\u094D\u0930\u094B\u092B\u093C\u093E\u0907\u0932",
  "theme.label": "\u0925\u0940\u092E",
  "theme.switchToDark": "\u0921\u093E\u0930\u094D\u0915 \u0938\u094D\u0915\u094D\u0930\u0940\u0928 \u092A\u0930 \u091C\u093E\u090F\u0901",
  "theme.switchToLight": "\u0932\u093E\u0907\u091F \u0938\u094D\u0915\u094D\u0930\u0940\u0928 \u092A\u0930 \u091C\u093E\u090F\u0901",
  "language.label": "\u092D\u093E\u0937\u093E",
  "auth.subtitle": "\u0905\u092A\u0928\u0947 \u092A\u094D\u0930\u0936\u093E\u0938\u0915 \u0926\u094D\u0935\u093E\u0930\u093E \u0926\u093F\u090F \u0917\u090F \u0906\u0908\u0921\u0940 \u0914\u0930 \u0935\u093F\u0935\u0930\u0923 \u0938\u0947 \u0938\u093E\u0907\u0928 \u0907\u0928 \u0915\u0930\u0947\u0902\u0964",
  "auth.userId": "\u092F\u0942\u091C\u093C\u0930 \u0906\u0908\u0921\u0940",
  "auth.emailOrPhone": "\u0908\u092E\u0947\u0932 \u092F\u093E \u092B\u093C\u094B\u0928",
  "auth.password": "\u092A\u093E\u0938\u0935\u0930\u094D\u0921",
  "auth.signIn": "\u0932\u0949\u0917 \u0907\u0928 \u0915\u0930\u0947\u0902",
  "auth.signingIn": "\u0938\u093E\u0907\u0928 \u0907\u0928 \u0939\u094B \u0930\u0939\u093E \u0939\u0948\u2026",
  "auth.welcome": "\u0938\u094D\u0935\u093E\u0917\u0924 \u0939\u0948, {name}",
  "auth.pendingApproval": "\u0938\u093E\u0907\u0928 \u0907\u0928 \u0939\u094B \u0917\u092F\u093E\u0964 \u0939\u093E\u091C\u093C\u093F\u0930\u0940 \u0926\u0930\u094D\u091C \u0915\u0930\u0928\u0947 \u0938\u0947 \u092A\u0939\u0932\u0947 \u0906\u092A\u0915\u0947 \u0916\u093E\u0924\u0947 \u0915\u094B \u092E\u0902\u091C\u093C\u0942\u0930\u0940 \u0915\u093E \u0907\u0902\u0924\u091C\u093C\u093E\u0930 \u0939\u0948\u0964",
  "auth.offline": "\u0938\u0930\u094D\u0935\u0930 \u0938\u0947 \u0915\u094B\u0908 \u0915\u0928\u0947\u0915\u094D\u0936\u0928 \u0928\u0939\u0940\u0902\u0964 \u0928\u0947\u091F\u0935\u0930\u094D\u0915 \u091C\u093E\u0901\u091A\u0947\u0902 \u0914\u0930 \u092B\u093F\u0930 \u0915\u094B\u0936\u093F\u0936 \u0915\u0930\u0947\u0902\u0964",
  "auth.rejected": "\u092F\u0939 \u0906\u0908\u0921\u0940, \u0908\u092E\u0947\u0932 \u092F\u093E \u092A\u093E\u0938\u0935\u0930\u094D\u0921 \u0938\u094D\u0935\u0940\u0915\u093E\u0930 \u0928\u0939\u0940\u0902 \u0939\u0941\u0906\u0964",
  "auth.forbidden": "\u092F\u0939 \u0916\u093E\u0924\u093E \u0938\u093E\u0907\u0928 \u0907\u0928 \u0928\u0939\u0940\u0902 \u0915\u0930 \u0938\u0915\u0924\u093E\u0964",
  "auth.tooMany": "\u092C\u0939\u0941\u0924 \u091C\u093C\u094D\u092F\u093E\u0926\u093E \u0915\u094B\u0936\u093F\u0936\u0947\u0902\u0964 \u090F\u0915 \u092E\u093F\u0928\u091F \u0930\u0941\u0915\u0915\u0930 \u0926\u094B\u092C\u093E\u0930\u093E \u0915\u094B\u0936\u093F\u0936 \u0915\u0930\u0947\u0902\u0964",
  "auth.failed": "\u0938\u093E\u0907\u0928 \u0907\u0928 \u0928\u0939\u0940\u0902 \u0939\u094B \u0938\u0915\u093E\u0964"
};
var UR = {
  "app.name": "\u062D\u0627\u0636\u0631\u06CC \u0627\u0644\u0645\u0648\u0642\u0639",
  "header.roleSignedOut": "\u0644\u0627\u06AF \u0627\u0646",
  "nav.sections": "\u062D\u0635\u06D2",
  "nav.clock": "\u06A9\u0644\u0627\u06A9",
  "nav.history": "\u062A\u0627\u0631\u06CC\u062E",
  "nav.alerts": "\u0627\u0644\u0631\u0679\u0633",
  "nav.notes": "\u0646\u0648\u0679\u0633",
  "nav.profile": "\u067E\u0631\u0648\u0641\u0627\u0626\u0644",
  "theme.label": "\u062A\u06BE\u06CC\u0645",
  "theme.switchToDark": "\u06AF\u06C1\u0631\u06CC \u0627\u0633\u06A9\u0631\u06CC\u0646 \u067E\u0631 \u062C\u0627\u0626\u06CC\u06BA",
  "theme.switchToLight": "\u0631\u0648\u0634\u0646 \u0627\u0633\u06A9\u0631\u06CC\u0646 \u067E\u0631 \u062C\u0627\u0626\u06CC\u06BA",
  "language.label": "\u0632\u0628\u0627\u0646",
  "auth.subtitle": "\u0627\u067E\u0646\u06D2 \u0627\u06CC\u0688\u0645\u0646\u0633\u0679\u0631\u06CC\u0679\u0631 \u06A9\u06D2 \u062F\u06CC\u06D2 \u06AF\u0626\u06D2 \u0622\u0626\u06CC \u0688\u06CC \u0627\u0648\u0631 \u062A\u0641\u0635\u06CC\u0644\u0627\u062A \u0633\u06D2 \u0644\u0627\u06AF \u0627\u0646 \u06A9\u0631\u06CC\u06BA\u06D4",
  "auth.userId": "\u0635\u0627\u0631\u0641 \u0622\u0626\u06CC \u0688\u06CC",
  "auth.emailOrPhone": "\u0627\u06CC \u0645\u06CC\u0644 \u06CC\u0627 \u0641\u0648\u0646",
  "auth.password": "\u067E\u0627\u0633 \u0648\u0631\u0688",
  "auth.signIn": "\u0644\u0627\u06AF \u0627\u0646",
  "auth.signingIn": "\u0644\u0627\u06AF \u0627\u0646 \u06C1\u0648 \u0631\u06C1\u0627 \u06C1\u06D2\u2026",
  "auth.welcome": "\u062E\u0648\u0634 \u0622\u0645\u062F\u06CC\u062F\u060C {name}",
  "auth.pendingApproval": "\u0644\u0627\u06AF \u0627\u0646 \u06C1\u0648 \u06AF\u06CC\u0627\u06D4 \u062D\u0627\u0636\u0631\u06CC \u062F\u0631\u062C \u06A9\u0631\u0646\u06D2 \u0633\u06D2 \u067E\u06C1\u0644\u06D2 \u0622\u067E \u06A9\u06D2 \u0627\u06A9\u0627\u0624\u0646\u0679 \u06A9\u0648 \u0645\u0646\u0638\u0648\u0631\u06CC \u06A9\u0627 \u0627\u0646\u062A\u0638\u0627\u0631 \u06C1\u06D2\u06D4",
  "auth.offline": "\u0633\u0631\u0648\u0631 \u0633\u06D2 \u06A9\u0648\u0626\u06CC \u0631\u0627\u0628\u0637\u06C1 \u0646\u06C1\u06CC\u06BA\u06D4 \u0646\u06CC\u0679 \u0648\u0631\u06A9 \u062F\u06CC\u06A9\u06BE\u06CC\u06BA \u0627\u0648\u0631 \u062F\u0648\u0628\u0627\u0631\u06C1 \u06A9\u0648\u0634\u0634 \u06A9\u0631\u06CC\u06BA\u06D4",
  "auth.rejected": "\u06CC\u06C1 \u0622\u0626\u06CC \u0688\u06CC\u060C \u0627\u06CC \u0645\u06CC\u0644 \u06CC\u0627 \u067E\u0627\u0633 \u0648\u0631\u0688 \u0642\u0628\u0648\u0644 \u0646\u06C1\u06CC\u06BA \u06C1\u0648\u0627\u06D4",
  "auth.forbidden": "\u06CC\u06C1 \u0627\u06A9\u0627\u0624\u0646\u0679 \u0644\u0627\u06AF \u0627\u0646 \u0646\u06C1\u06CC\u06BA \u06A9\u0631 \u0633\u06A9\u062A\u0627\u06D4",
  "auth.tooMany": "\u0628\u06C1\u062A \u0632\u06CC\u0627\u062F\u06C1 \u06A9\u0648\u0634\u0634\u06CC\u06BA\u06D4 \u0627\u06CC\u06A9 \u0645\u0646\u0679 \u0631\u06A9 \u06A9\u0631 \u062F\u0648\u0628\u0627\u0631\u06C1 \u06A9\u0648\u0634\u0634 \u06A9\u0631\u06CC\u06BA\u06D4",
  "auth.failed": "\u0644\u0627\u06AF \u0627\u0646 \u0646\u0627\u06A9\u0627\u0645 \u0631\u06C1\u0627\u06D4"
};
var TABLES = { ar: AR, en: EN, hi: HI, ur: UR };
function t(key, vars) {
  const table = TABLES[getLocale()] ?? EN;
  const template = table[key] ?? EN[key];
  if (!vars) return template;
  return template.replace(
    /\{(\w+)\}/g,
    (match, name) => name in vars ? String(vars[name]) : match
  );
}

// ../../mobile-client/src/ui/dom.ts
var ENTITIES = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;"
};
function esc(value) {
  if (value === null || value === void 0) return "";
  return String(value).replace(/[&<>"']/g, (ch) => ENTITIES[ch] ?? ch);
}
function el(markup) {
  const wrapper = document.createElement("div");
  wrapper.innerHTML = markup.trim();
  return wrapper.firstElementChild;
}
function parseServerTs(value) {
  if (!value) return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})/.exec(String(value));
  if (!m) return null;
  return new Date(
    Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6])
  );
}
var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function fmtDateTime(value) {
  const d = parseServerTs(value);
  if (!d) return "\u2014";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getUTCDate())} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}, ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}
function fmtAgo(from) {
  if (!from) return "never";
  const seconds = Math.max(0, Math.round((Date.now() - from.getTime()) / 1e3));
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}
function on(host2, type, handler, options) {
  host2.addEventListener(type, handler, options);
  return () => host2.removeEventListener(type, handler, options);
}

// ../../mobile-client/src/ui/icons.ts
var PATHS = {
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5l3.2 2"/>',
  history: '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1"/><path d="M3.2 4.6v4h4"/><path d="M12 8v4.4l3 1.9"/>',
  alerts: '<path d="M6.5 10a5.5 5.5 0 0 1 11 0c0 4 1.5 5.5 1.5 5.5H5S6.5 14 6.5 10Z"/><path d="M10 18.5a2.2 2.2 0 0 0 4 0"/>',
  notes: '<path d="M5 4.5h9.5L19 9v10.5H5z"/><path d="M14 4.5V9h5"/><path d="M8.5 13h7M8.5 16.5h4.5"/>',
  profile: '<circle cx="12" cy="8.5" r="3.6"/><path d="M4.8 20c0-3.7 3.2-6.2 7.2-6.2s7.2 2.5 7.2 6.2"/>',
  camera: '<path d="M3.5 8.5h3l1.5-2.2h8L17.5 8.5h3v10.5h-17z"/><circle cx="12" cy="13.5" r="3.4"/>',
  // The theme toggle's two glyphs, drawn with the console's own moon (its
  // ``ADMIN_ICONS.theme``) so the desk's dark-mode button and the handset's are the
  // same picture. The gear the header used to carry is gone with the server sheet.
  moon: '<path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 3.2v2.2M12 18.6v2.2M3.2 12h2.2M18.6 12h2.2M5.8 5.8l1.6 1.6M16.6 16.6l1.6 1.6M5.8 18.2l1.6-1.6M16.6 7.4l1.6-1.6"/>',
  refresh: '<path d="M20 11.5a8 8 0 1 0-2.4 6.2"/><path d="M20.3 4.5v4.2h-4.2"/>',
  logout: '<path d="M14 4.5H6.5v15H14"/><path d="M11.5 12h9M17.5 8.5 21 12l-3.5 3.5"/>',
  check: '<path d="M4.5 12.5 9.5 17.5 19.5 6.5"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  chevron: '<path d="M9 5.5 15.5 12 9 18.5"/>',
  pin: '<path d="M12 21s6.5-5.4 6.5-10.2A6.5 6.5 0 0 0 5.5 10.8C5.5 15.6 12 21 12 21Z"/><circle cx="12" cy="10.6" r="2.4"/>',
  key: '<circle cx="8" cy="12" r="3.5"/><path d="M11.5 12h9M17.5 12v3.2M20.5 12v2.2"/>',
  shield: '<path d="M12 3.5 19 6v6c0 4.2-3 7.3-7 8.5-4-1.2-7-4.3-7-8.5V6z"/><path d="M9 12.2l2.2 2.2L15.5 10"/>',
  wifiOff: '<path d="M3.5 8.5A13 13 0 0 1 12 5.2M20.5 8.5a13 13 0 0 0-4.6-2.6"/><path d="M7 12.4a9 9 0 0 1 3.2-1.4M17 12.4a9 9 0 0 0-2.2-1.1"/><path d="M10 15.9a4.5 4.5 0 0 1 4 0"/><path d="M12 19.2h.01"/><path d="M3 3l18 18"/>',
  cloudUp: '<path d="M7 18.5a4.2 4.2 0 0 1-.3-8.4A5.5 5.5 0 0 1 17.2 9a3.8 3.8 0 0 1 .6 7.5"/><path d="M12 21v-8M9 16l3-3 3 3"/>',
  alert: '<path d="M12 4.5 21 20H3z"/><path d="M12 10v4.2M12 17h.01"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.8h.01"/>',
  route: '<circle cx="6.5" cy="6.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/><path d="M6.5 9v4.5a4 4 0 0 0 4 4h4.5"/>',
  user: '<circle cx="12" cy="8.5" r="3.6"/><path d="M4.8 20c0-3.7 3.2-6.2 7.2-6.2s7.2 2.5 7.2 6.2"/>',
  filter: '<path d="M4 6h16M7 12h10M10 18h4"/>'
};
function icon(name, size = 24, className = "hand-icon") {
  const body = PATHS[name] ?? "";
  return `<svg class="${className}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${body}</svg>`;
}

// ../../mobile-client/src/ui/components/focus.ts
var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
function trapFocus(root, opts = {}) {
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const focusable = () => Array.from(root.querySelectorAll(FOCUSABLE)).filter(
    (node) => node.getClientRects().length > 0
  );
  const raf = requestAnimationFrame(() => {
    const list = focusable();
    if (list.length) (opts.initial === "last" ? list[list.length - 1] : list[0]).focus();
    else {
      root.tabIndex = -1;
      root.focus();
    }
  });
  const stop = on(document, "keydown", (event) => {
    if (event.key !== "Tab") return;
    const list = focusable();
    if (!list.length) {
      event.preventDefault();
      return;
    }
    const first = list[0];
    const last = list[list.length - 1];
    const active = document.activeElement;
    if (event.shiftKey) {
      if (active === first || !root.contains(active)) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last || !root.contains(active)) {
      event.preventDefault();
      first.focus();
    }
  });
  let released = false;
  return {
    release: () => {
      if (released) return;
      released = true;
      cancelAnimationFrame(raf);
      stop();
      if (previous?.isConnected) previous.focus();
    }
  };
}

// ../../mobile-client/src/ui/components/sheet.ts
var current2 = null;
var sheetSeq = 0;
function openSheet(opts) {
  if (current2) current2.close();
  const dismissible = opts.dismissible !== false;
  const titleId = `hand-sheet-title-${++sheetSeq}`;
  const root = el(`
    <div class="hand-sheet" role="dialog" aria-modal="true" aria-labelledby="${titleId}">
      <div class="hand-sheet__scrim"></div>
      <div class="hand-sheet__panel">
        <div class="hand-sheet__grip"></div>
        <div class="hand-sheet__head">
          <span class="hand-sheet__title" id="${titleId}">${esc(opts.title)}</span>
          ${dismissible ? `<button class="ui-btn ui-btn-quiet is-icon" type="button" data-close aria-label="Close">${icon("close", 22)}</button>` : ""}
        </div>
        <div class="hand-sheet__body"></div>
        ${opts.actions?.length ? '<div class="hand-sheet__foot"></div>' : ""}
      </div>
    </div>
  `);
  const bodyHost = root.querySelector(".hand-sheet__body");
  if (typeof opts.body === "string") bodyHost.innerHTML = opts.body;
  else if (opts.body instanceof HTMLElement) bodyHost.appendChild(opts.body);
  else opts.body(bodyHost);
  const buttons = [];
  const foot = root.querySelector(".hand-sheet__foot");
  if (foot && opts.actions) {
    for (const [index, action] of opts.actions.entries()) {
      const kind = action.kind ?? (index === 0 ? "primary" : "plain");
      const cls = kind === "primary" ? "ui-btn ui-btn-primary" : kind === "danger" ? "ui-btn ui-btn-danger" : kind === "ghost" ? "ui-btn ui-btn-quiet" : "ui-btn";
      const button = el(`<button class="${cls}" type="button"><span class="ui-btn__label">${esc(action.label)}</span></button>`);
      if (action.busy) {
        button.innerHTML = `<span class="hand-spinner"></span><span class="ui-btn__label">${esc(action.label)}</span>`;
      }
      button.addEventListener("click", () => {
        void action.onClick(handle);
      });
      buttons.push(button);
      foot.appendChild(button);
    }
  }
  document.body.appendChild(root);
  const trap = trapFocus(root, { initial: opts.initialFocus ?? "first" });
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.add("hand-sheet--in")));
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    root.classList.remove("hand-sheet--in");
    if (current2 === handle) current2 = null;
    trap.release();
    disposers.forEach((off) => off());
    window.setTimeout(() => root.remove(), 220);
    opts.onClose?.();
  };
  const handle = {
    root,
    dismissible,
    close,
    setBusy(index, busy) {
      const button = buttons[index];
      if (!button) return;
      const label = button.querySelector(".ui-btn__label")?.textContent ?? "";
      button.innerHTML = busy ? `<span class="hand-spinner"></span><span class="ui-btn__label">${esc(label)}</span>` : `<span class="ui-btn__label">${esc(label)}</span>`;
      button.toggleAttribute("disabled", busy);
    },
    setDisabled(index, disabled) {
      buttons[index]?.toggleAttribute("disabled", disabled);
    }
  };
  const disposers = [];
  if (dismissible) {
    disposers.push(
      on(root.querySelector(".hand-sheet__scrim"), "click", close),
      on(root.querySelector("[data-close]"), "click", close)
    );
  }
  disposers.push(
    on(document, "keydown", (event) => {
      if (event.key === "Escape" && dismissible) close();
    })
  );
  current2 = handle;
  return handle;
}
function confirmDialog(opts) {
  return new Promise((resolve) => {
    let answered = false;
    const handle = openSheet({
      title: opts.title,
      body: `
        <p class="hand-dim">${esc(opts.message)}</p>
        ${opts.detail ? rawBlock(opts.detail) : ""}
      `,
      dismissible: true,
      // A destructive question opens on Cancel: Enter should never clock someone out by
      // reflex. A plain confirmation opens on its first control as usual.
      initialFocus: opts.danger ? "last" : "first",
      actions: [
        {
          label: opts.confirmLabel ?? "Confirm",
          kind: opts.danger ? "danger" : "primary",
          onClick: (sheet) => {
            answered = true;
            sheet.close();
            resolve(true);
          }
        },
        {
          label: opts.cancelLabel ?? "Cancel",
          kind: "ghost",
          onClick: (sheet) => {
            sheet.close();
          }
        }
      ],
      onClose: () => {
        if (!answered) resolve(false);
      }
    });
    void handle;
  });
}
function rawBlock(markup) {
  return `<div class="hand-stack">${markup}</div>`;
}

// ../../mobile-client/src/ui/theme.ts
var THEME_STORAGE_KEY = "app_theme";
var THEME_PREFERENCE_KEY = "app_theme";
var LEGACY_THEME_PREFERENCE_KEY = "ui_theme_dark";
var listeners = /* @__PURE__ */ new Set();
function applyTheme(theme) {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.classList.toggle("dark", theme === "dark");
  root.setAttribute("data-theme", theme);
}
function getTheme() {
  if (typeof document === "undefined") return "light";
  return document.documentElement.classList.contains("dark") ? "dark" : "light";
}
function isDarkTheme() {
  return getTheme() === "dark";
}
function onThemeChange(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
function remember(theme) {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
  }
  void Preferences.set({ key: THEME_PREFERENCE_KEY, value: theme }).catch(() => {
  });
  void Preferences.remove({ key: LEGACY_THEME_PREFERENCE_KEY }).catch(() => {
  });
}
function setTheme(theme) {
  applyTheme(theme);
  remember(theme);
  for (const listener of [...listeners]) listener();
}
async function saveTheme(dark) {
  setTheme(dark ? "dark" : "light");
}

// ../../mobile-client/src/ui/components/preferences.ts
var __test__ = { LOCALE_CODES: LOCALES.map((locale) => locale.code) };

// ../../mobile-client/src/ui/shell.ts
var TAB_META = {
  clock: { get label() {
    return t("nav.clock");
  }, icon: "clock" },
  history: { get label() {
    return t("nav.history");
  }, icon: "history" },
  alerts: { get label() {
    return t("nav.alerts");
  }, icon: "alerts" },
  notes: { get label() {
    return t("nav.notes");
  }, icon: "notes" },
  profile: { get label() {
    return t("nav.profile");
  }, icon: "profile" }
};
var tabsHost = null;
var unreadAlerts = 0;
var unreadNotes = 0;
var liveRegion = null;
var badgesSeen = false;
function announce(message) {
  if (liveRegion) liveRegion.textContent = message;
}
function setBadges(counts) {
  const before = { alerts: unreadAlerts, notes: unreadNotes };
  if (counts.alerts !== void 0) unreadAlerts = Math.max(0, counts.alerts);
  if (counts.notes !== void 0) unreadNotes = Math.max(0, counts.notes);
  if (badgesSeen) {
    if (counts.alerts !== void 0 && unreadAlerts > before.alerts) {
      announce(`${TAB_META.alerts.label}: ${unreadAlerts} unread`);
    }
    if (counts.notes !== void 0 && unreadNotes > before.notes) {
      announce(`${TAB_META.notes.label}: ${unreadNotes} unread`);
    }
  }
  badgesSeen = true;
  paintTabs();
}
function paintTabs() {
  if (!tabsHost) return;
  tabsHost.setAttribute("aria-label", t("nav.sections"));
  const route = router.getRoute();
  for (const tab of TAB_IDS) {
    const button = tabsHost.querySelector(`[data-tab="${tab}"]`);
    if (!button) continue;
    if (tab === route.tab) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
    const label = button.querySelector(".hand-tab-label");
    if (label) label.textContent = TAB_META[tab].label;
    const badgeCount = tab === "alerts" ? unreadAlerts : tab === "notes" ? unreadNotes : 0;
    let badge = button.querySelector(".hand-tab-count");
    let status = button.querySelector(".hand-tab-status");
    if (badgeCount > 0) {
      if (!badge) {
        badge = el('<span class="hand-tab-count" aria-hidden="true"></span>');
        button.appendChild(badge);
      }
      badge.setAttribute("data-count", String(badgeCount));
      badge.textContent = badgeCount > 99 ? "99+" : String(badgeCount);
      if (!status) {
        status = el('<span class="hand-sr-only hand-tab-status"></span>');
        button.appendChild(status);
      }
      status.textContent = badgeCount > 99 ? "more than 99 unread" : `${badgeCount} unread`;
    } else {
      badge?.remove();
      status?.remove();
    }
  }
}

// ../../mobile-client/src/core/sync-loop.ts
var inFlight = null;
async function refreshShiftState() {
  try {
    const stats = await request({ path: "/worker/me/stats" });
    const session = stats.active_session ?? null;
    store.setShift({
      active: Boolean(session),
      siteName: session?.site_name ?? null,
      clockInTime: session?.clock_in_time ?? null,
      // ``in_transit`` is what makes the next tap a "confirm arrival" rather than a
      // clock-out, so it is carried rather than inferred from the site name — the
      // placeholder is a sentence for a human, and matching on English is how a reworded
      // placeholder becomes a missing button.
      startSource: session?.in_transit ? "transit" : null
    });
  } catch {
  }
}
async function refreshBadges() {
  try {
    const [alerts, notes] = await Promise.all([
      request({
        path: "/worker/me/notifications",
        query: { limit: 1, unread_only: true }
      }),
      request({ path: "/worker/notes", query: { limit: 1 } })
    ]);
    setBadges({ alerts: Number(alerts.unread ?? 0), notes: Number(notes.unread ?? 0) });
  } catch {
  }
}
async function runSync(workerId, deviceId) {
  if (inFlight) return null;
  inFlight = (async () => {
    const summary = await syncNow(workerId, deviceId);
    store.setOfflineQueueDepth(summary.pending);
    if (summary.at) store.setLastSyncAt(summary.at);
    if (summary.sent > 0) {
      await refreshShiftState();
      await refreshBadges();
    }
    return summary;
  })();
  try {
    return await inFlight;
  } finally {
    inFlight = null;
  }
}
async function syncForCurrentWorker() {
  const session = store.getState().session;
  if (!session) return null;
  const device = await ensureDevice(session.user.id).catch(() => null);
  if (!device) return null;
  return runSync(session.user.id, device.device_id);
}

// ../../mobile-client/src/ui/components/toast.ts
var DEFAULT_DURATION = {
  info: 3200,
  ok: 3200,
  warn: 5200,
  error: 6500
};
var host = null;
function ensureHost() {
  if (host && host.isConnected) return host;
  host = document.createElement("div");
  host.className = "hand-toasts";
  host.setAttribute("role", "status");
  host.setAttribute("aria-live", "polite");
  document.body.appendChild(host);
  return host;
}
function toast(message, opts = {}) {
  const kind = opts.kind ?? "info";
  const root = ensureHost();
  const node = document.createElement("div");
  node.className = `hand-toast hand-toast--${kind}`;
  node.innerHTML = `<span class="hand-toast__text">${esc(message)}</span>` + (opts.action ? `<button class="ui-btn ui-btn-sm" type="button">${esc(opts.action.label)}</button>` : "");
  root.appendChild(node);
  let removed = false;
  const remove = () => {
    if (removed) return;
    removed = true;
    node.classList.add("hand-toast--out");
    window.setTimeout(() => node.remove(), 220);
  };
  if (opts.action) {
    node.querySelector("button")?.addEventListener("click", (event) => {
      event.stopPropagation();
      opts.action?.onClick();
      remove();
    });
  }
  if (!opts.action) {
    const duration = opts.durationMs ?? DEFAULT_DURATION[kind];
    if (duration > 0) window.setTimeout(remove, duration);
  } else {
    node.addEventListener("click", remove);
  }
  return remove;
}
var toastOk = (message, opts) => toast(message, { ...opts, kind: "ok" });
var toastError = (message, opts) => toast(message, { ...opts, kind: "error" });
if (typeof document !== "undefined" && document.body) ensureHost();

// ../../mobile-client/src/ui/screens/profile/profile.ts
function createProfileScreen(onSignedOut) {
  return {
    title: () => "Profile",
    subtitle: () => store.getState().session?.user.name ?? "",
    mount(host2, ctx) {
      let device = { device_id: null, key_epoch: null, registered_at: null };
      let anchor = null;
      let queueDepth = 0;
      let serverUrl = "";
      let deviceState = "unknown";
      let deviceNote = "Checking\u2026";
      let lastSyncAt = null;
      const disposers = [];
      host2.innerHTML = `<div id="profile-root"></div>`;
      const root = host2.querySelector("#profile-root");
      async function loadDevice2() {
        const session = store.getState().session;
        if (!session) return;
        try {
          const record = await ensureDevice(session.user.id);
          device = {
            device_id: record.device_id,
            key_epoch: record.key_epoch,
            registered_at: record.registered_at
          };
          const state = await anchorState(session.user.id);
          anchor = state.anchor ? {
            anchor_id: state.anchor.anchor_id,
            fetched_at: state.anchor.fetched_at,
            age_seconds: state.age_seconds,
            expires_in_seconds: state.expires_in_seconds,
            max_offline_hours: state.anchor.max_offline_hours,
            fresh: state.fresh
          } : null;
          queueDepth = await pendingCount(session.user.id);
          if (!state.anchor) {
            deviceState = "attention";
            deviceNote = "No time anchor held. Connect once to enable offline punches.";
          } else if (!state.fresh) {
            deviceState = "attention";
            deviceNote = "The time anchor has expired. Connect to renew it.";
          } else {
            deviceState = "ready";
            deviceNote = `Ready \u2014 offline punches valid for ${Math.max(0, Math.round(state.expires_in_seconds / 3600))} h more.`;
          }
        } catch (e) {
          const err = e;
          deviceState = "attention";
          deviceNote = err.code === "device_key_lost" ? "This device is registered but its signing key is gone. Re-register it below." : err.message ?? "The device state could not be read.";
        }
        paint();
      }
      function renderDevicePanel() {
        const pct = anchor && anchor.max_offline_hours > 0 ? Math.max(0, Math.min(100, Math.round(anchor.expires_in_seconds / (anchor.max_offline_hours * 3600) * 100))) : 0;
        const fillClass = !anchor || !anchor.fresh ? "device-anchor__fill--expired" : pct < 25 ? "device-anchor__fill--low" : "";
        return `
          <div class="hand-card">
            <div class="hand-section-head">
              <h2 class="hand-section-title">This phone</h2>
              <span class="ui-badge ${deviceState === "ready" ? "is-live" : deviceState === "attention" ? "is-warn" : ""}">
                ${deviceState === "ready" ? "Ready" : deviceState === "attention" ? "Attention" : "Checking"}
              </span>
            </div>
            <p class="hand-section-note">${esc(deviceNote)}</p>

            ${anchor ? `<div class="device-anchor">
                     <div class="device-anchor__bar">
                       <div class="device-anchor__fill ${fillClass}" style="width:${pct}%"></div>
                     </div>
                     <div class="device-anchor__meta">
                       <span>Anchor issued ${esc(fmtAgo(parseServerTs(anchor.fetched_at)))}</span>
                       <span>${anchor.fresh ? `${Math.max(0, Math.round(anchor.expires_in_seconds / 3600))} h left` : "expired"}</span>
                     </div>
                   </div>` : ""}

            <dl class="hand-dl">
              <div class="hand-dl-row">
                <dt>Device</dt>
                <dd class="is-mono">${esc(device.device_id ?? "Not registered")}</dd>
              </div>
              ${device.key_epoch !== null ? `<div class="hand-dl-row">
                       <dt>Signing key</dt>
                       <dd class="is-mono">#${esc(String(device.key_epoch))}</dd>
                     </div>` : ""}
              <div class="hand-dl-row">
                <dt>Punches waiting to sync</dt>
                <dd class="is-mono">${esc(String(queueDepth))}</dd>
              </div>
            </dl>

            <div class="ui-row" style="margin-top:12px">
              <button class="ui-btn ui-btn-quiet is-grow" type="button" data-action="sync">
                ${icon("refresh", 16)} Sync now
              </button>
              <button class="ui-btn ui-btn-quiet is-grow" type="button" data-action="rotate">
                ${icon("key", 16)} Re-register
              </button>
            </div>
          </div>
        `;
      }
      function renderAccount() {
        const session = store.getState().session;
        if (!session) return "";
        const user = session.user;
        return `
          <div class="hand-card">
            <div class="hand-section-head">
              <h2 class="hand-section-title">Signed in as</h2>
              <span class="ui-badge">${esc(user.role.replace(/_/g, " "))}</span>
            </div>
            <dl class="hand-dl">
              <div class="hand-dl-row">
                <dt>Name</dt>
                <dd>${esc(user.name)}</dd>
              </div>
              <div class="hand-dl-row">
                <dt>Worker ID</dt>
                <dd class="is-mono">${esc(user.id)}</dd>
              </div>
              ${user.email ? `<div class="hand-dl-row"><dt>Email</dt><dd>${esc(user.email)}</dd></div>` : ""}
              ${user.phone ? `<div class="hand-dl-row"><dt>Phone</dt><dd>${esc(user.phone)}</dd></div>` : ""}
            </dl>
          </div>
        `;
      }
      function renderRows() {
        const session = store.getState().session;
        const expires = session?.expiresAt ? fmtDateTime(session.expiresAt.slice(0, 19).replace("T", " ")) : "\u2014";
        return `
          <div class="hand-card">
            <div class="hand-section-head">
              <h2 class="hand-section-title">This session</h2>
            </div>
            <dl class="hand-dl">
              <div class="hand-dl-row">
                <dt>Expires</dt>
                <dd>${esc(expires)}</dd>
              </div>
              <div class="hand-dl-row">
                <dt>Server</dt>
                <dd class="is-mono">${esc(serverUrl)}</dd>
              </div>
              <div class="hand-dl-row">
                <dt>Last sync</dt>
                <dd>${esc(lastSyncAt ? fmtAgo(parseServerTs(lastSyncAt)) : "never")}</dd>
              </div>
            </dl>
          </div>
        `;
      }
      function renderActions() {
        return `
          <div class="hand-card">
            <dl class="hand-dl">
              <button class="hand-dl-row is-action" type="button" data-action="password"
                      aria-label="Change password">
                <dt aria-hidden="true">${icon("key", 16)} Change password</dt>
                <dd aria-hidden="true">${icon("chevron", 16)}</dd>
              </button>
              <button class="hand-dl-row is-action" type="button" data-action="delete-account"
                      aria-label="Delete my account">
                <dt aria-hidden="true">${icon("logout", 16)} Delete my account</dt>
                <dd aria-hidden="true">${icon("chevron", 16)}</dd>
              </button>
            </dl>
          </div>
        `;
      }
      function renderSwitches() {
        return `
          <div class="hand-card">
            <div class="hand-section-head">
              <h2 class="hand-section-title">Screen</h2>
            </div>
            <button class="hand-switch" type="button" role="switch"
                    aria-checked="${isDarkTheme() ? "true" : "false"}" data-action="theme">
              <span class="hand-switch__text">
                <span class="hand-switch__label">Dark screen</span>
                <span class="hand-switch__hint">Easier at night, harder in direct sunlight.</span>
              </span>
              <span class="hand-switch__track" aria-hidden="true"><span class="hand-switch__thumb"></span></span>
            </button>
          </div>
        `;
      }
      function renderSignOut() {
        return `
          <button class="ui-btn ui-btn-danger is-block" type="button" data-action="signout">
            ${icon("logout", 16)} Sign out
          </button>
          <p class="hand-faint" style="text-align:center;font-size:12px">
            ${queueDepth > 0 ? `${queueDepth} punch${queueDepth === 1 ? "" : "es"} still waiting on this phone will stay here until they sync.` : ""}
          </p>
          <p class="hand-faint" style="text-align:center;font-size:12px">
            Attendance mobile \xB7 offline signing v1
          </p>
        `;
      }
      function paint() {
        root.innerHTML = `
          <div class="hand-screen">
            ${renderAccount()}
            ${renderDevicePanel()}
            ${renderSwitches()}
            ${renderRows()}
            ${renderActions()}
            ${renderSignOut()}
          </div>
        `;
      }
      async function changePassword() {
        openSheet({
          title: "Change password",
          body: `
            <div class="hand-field-group">
              <label class="hand-field-label" for="pw-current">Current password</label>
              <input class="ui-field" id="pw-current" type="password" autocomplete="current-password" />
            </div>
            <div class="hand-field-group">
              <label class="hand-field-label" for="pw-new">New password</label>
              <input class="ui-field" id="pw-new" type="password" autocomplete="new-password" />
              <span class="ui-hint">At least 8 characters.</span>
            </div>
            <div class="hand-field-group">
              <label class="hand-field-label" for="pw-confirm">Repeat new password</label>
              <input class="ui-field" id="pw-confirm" type="password" autocomplete="new-password" />
              <span class="ui-field-error" data-error hidden></span>
            </div>
            <div class="hand-alert is-info">
              ${icon("info", 16)}
              <p>Changing your password signs out every device, including this one.</p>
            </div>
          `,
          actions: [
            {
              label: "Change password",
              kind: "primary",
              onClick: (sheet) => {
                void submitPassword(sheet);
              }
            },
            { label: "Cancel", kind: "ghost", onClick: (sheet) => sheet.close() }
          ]
        });
        async function submitPassword(sheet) {
          const current3 = sheet.root.querySelector("#pw-current").value;
          const next = sheet.root.querySelector("#pw-new").value;
          const confirm = sheet.root.querySelector("#pw-confirm").value;
          const error = sheet.root.querySelector("[data-error]");
          const fail = (message) => {
            error.textContent = message;
            error.hidden = false;
          };
          error.hidden = true;
          if (!current3) return fail("Enter your current password.");
          if (next.length < 8) return fail("The new password must be at least 8 characters.");
          if (next !== confirm) return fail("The two new passwords do not match.");
          sheet.setBusy(0, true);
          try {
            await request({
              method: "POST",
              path: "/auth/change_password",
              body: { current_password: current3, new_password: next }
            });
            sheet.close();
            toastOk("Password updated. Please sign in again.");
            await clearPersistedSession();
            store.setSession(null);
            onSignedOut();
          } catch (e) {
            const err = e;
            sheet.setBusy(0, false);
            fail(err.status === 401 ? "That current password is not right." : err.message ?? "The password could not be changed.");
          }
        }
      }
      async function deleteAccount() {
        openSheet({
          title: "Delete my account",
          body: `
            <div class="hand-field-group">
              <label class="hand-field-label" for="del-password">Your password</label>
              <input class="ui-field" id="del-password" type="password" autocomplete="current-password" />
              <span class="ui-field-error" data-error hidden></span>
            </div>
            <div class="hand-alert is-danger">
              ${icon("info", 16)}
              <p>
                Your name, contact details, the photo on file and this phone's signing key are
                removed, and you are signed out everywhere. Shifts you have already worked are
                kept: the hours in them are owed to you, and they are what payroll is paid from.
              </p>
            </div>
            ${queueDepth > 0 ? `<div class="hand-alert is-danger">
                     ${icon("info", 16)}
                     <p>
                       ${queueDepth} punch${queueDepth === 1 ? "" : "es"} on this phone
                       ${queueDepth === 1 ? "has" : "have"} not synced yet. They are discarded
                       with the account.
                     </p>
                   </div>` : ""}
            <p class="hand-faint" style="text-align:center;font-size:12px">
              This cannot be undone.
            </p>
          `,
          actions: [
            {
              label: "Delete my account",
              kind: "danger",
              onClick: (sheet) => {
                void submitDelete(sheet);
              }
            },
            { label: "Keep my account", kind: "ghost", onClick: (sheet) => sheet.close() }
          ]
        });
        async function submitDelete(sheet) {
          const password = sheet.root.querySelector("#del-password").value;
          const error = sheet.root.querySelector("[data-error]");
          const fail = (message) => {
            error.textContent = message;
            error.hidden = false;
          };
          error.hidden = true;
          if (!password) return fail("Enter your password to confirm.");
          sheet.setBusy(0, true);
          try {
            const answer = await request({
              method: "POST",
              path: "/auth/delete_account",
              body: { current_password: password }
            });
            sheet.close();
            toastOk(answer?.message ?? "Your account has been deleted.");
            await clearPersistedSession();
            store.setSession(null);
            onSignedOut();
          } catch (e) {
            const err = e;
            sheet.setBusy(0, false);
            fail(
              err.status === 401 ? "That password is not right." : err.message ?? "The account could not be deleted."
            );
          }
        }
      }
      async function rotate() {
        const confirmed = await confirmDialog({
          title: "Re-register this phone?",
          message: "This gives the phone a new signing key. Anything still queued on it must sync first, or it will lose its signature.",
          confirmLabel: "Re-register",
          danger: true
        });
        if (!confirmed) return;
        const session = store.getState().session;
        if (!session) return;
        try {
          await rotateDevice(session.user.id);
          toastOk("This phone was re-registered.");
          await loadDevice2();
        } catch (e) {
          const err = e;
          toastError(
            err.code === "queue_not_empty" ? err.message ?? "Sync the waiting punches first." : err.message ?? "The device could not be re-registered."
          );
        }
      }
      async function signOut() {
        const confirmed = await confirmDialog({
          title: "Sign out?",
          message: queueDepth > 0 ? `${queueDepth} punch${queueDepth === 1 ? "" : "es"} have not synced. They stay on this phone, but they need a sign-in to upload.` : "You will need your ID and password to sign in again.",
          confirmLabel: "Sign out",
          danger: true
        });
        if (!confirmed) return;
        await clearPersistedSession();
        store.setSession(null);
        toastOk("Signed out.");
        onSignedOut();
      }
      disposers.push(
        on(root, "click", (event) => {
          const target = event.target.closest("[data-action]");
          if (!target) return;
          const action = target.getAttribute("data-action");
          if (action === "sync") {
            void (async () => {
              toastOk("Syncing\u2026");
              const summary = await syncForCurrentWorker();
              if (summary && summary.error) {
                toastError(summary.message ?? "Sync did not finish.");
              } else {
                toastOk("Everything is synced.");
              }
              await loadDevice2();
            })();
          } else if (action === "rotate") void rotate();
          else if (action === "theme") void saveTheme(!isDarkTheme());
          else if (action === "password") void changePassword();
          else if (action === "delete-account") void deleteAccount();
          else if (action === "signout") void signOut();
        })
      );
      disposers.push(onThemeChange(() => paint()));
      void (async () => {
        try {
          serverUrl = await resolveApiBaseUrl();
        } catch {
          serverUrl = "not configured";
        }
        lastSyncAt = store.getState().lastSyncAt;
        await loadDevice2();
      })();
      ctx.main.scrollTop = 0;
      return () => disposers.forEach((off) => off());
    }
  };
}

// ../../mobile-client/src/ui/__probe_harness.ts
window.MobileProfile = { createProfileScreen, store };
/*! Bundled license information:

@capacitor/core/dist/index.js:
  (*! Capacitor: https://capacitorjs.com/ - MIT License *)
*/
