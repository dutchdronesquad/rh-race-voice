/// <reference types="vite/client" />
declare const __SENDSPIN_VERSION__: string;

interface ImportMetaEnv {
  readonly VITE_SERVICE_VERSION?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
