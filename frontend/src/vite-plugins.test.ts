import { describe, expect, it } from "vitest"

import { stripJsCodeTrailingWhitespace, stripJsTrailingWhitespace } from "./vite-plugins"

describe("stripJsTrailingWhitespace", () => {
  it("normalizes JavaScript in renderChunk before hashing", () => {
    expect(stripJsCodeTrailingWhitespace("const value = 1  \n  \n"))
      .toBe("const value = 1\n\n")
    expect(stripJsTrailingWhitespace().renderChunk).toBeDefined()
  })

  it("does not expose an asset mutation hook", () => {
    const plugin = stripJsTrailingWhitespace()
    const assets = {
      "index.html": "<div>  </div>  ",
      "icon.svg": "<svg>  </svg>  ",
      "manifest.json": '{"name":"app"}  ',
    }
    expect(plugin.generateBundle).toBeUndefined()
    expect(assets).toEqual({
      "index.html": "<div>  </div>  ",
      "icon.svg": "<svg>  </svg>  ",
      "manifest.json": '{"name":"app"}  ',
    })
  })
})
