/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_HYPERION_API?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
