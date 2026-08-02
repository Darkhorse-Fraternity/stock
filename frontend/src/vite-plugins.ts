import type { Plugin } from "vite"

/** Normalize only rendered JavaScript chunks, before Rollup computes file hashes. */
export function stripJsCodeTrailingWhitespace(code: string): string {
  return code.replace(/[\t ]+$/gm, "")
}

export function stripJsTrailingWhitespace(): Plugin {
  return {
    name: "strip-js-trailing-whitespace",
    enforce: "post",
    renderChunk: {
      order: "post",
      handler(code) {
        return {
          code: stripJsCodeTrailingWhitespace(code),
          map: null,
        }
      },
    },
  }
}
