/// <reference types="vite/client" />

/** Set by vite.config.ts: the configured development backend origin, or "" in
 *  a production build (which always talks to its own origin). */
declare const __MC5_DEV_BACKEND__: string;

/** True only for `vite` (the development server), false for `vite build`. */
declare const __MC5_DEV_SERVER__: boolean;
