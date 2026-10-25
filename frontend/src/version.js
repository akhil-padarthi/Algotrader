/* global __APP_VERSION__ */
// Stamped at build time from package.json; compared with the server's version on load.
export const APP_VERSION = typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : 'dev';
