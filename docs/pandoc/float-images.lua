--- Anchor standalone screenshots beside their text, flush right at half width.
---
--- The README embeds each screenshot as an image on its own line, which pandoc
--- turns into a full-width, centred figure. For the printed User Guide we want the
--- body text on the left and the screenshot pinned to the right margin at half the
--- text width, anchored inside the section it documents, e.g.
---
---     ## Colour
---     bla bla bla bla    +-------+
---     bla bla bla bla    |  img  |
---     bla bla bla        +-------+
---
--- An earlier version floated the images with wrapfig. That looked right when an
--- image was about as tall as the surrounding text, but Labelmaker's node captures
--- are tall (lots of dark DAG margin) while most sections have only a line or two
--- of text — so the floats overflowed down into the *next* section, got deferred
--- to the following page detached from their text, and collided with the page
--- footer. wrapfig cannot be anchored, so we do not use it.
---
--- Instead each screenshot is paired with an adjacent paragraph in a two-column
--- `minipage` row: text in the left column, image in the right. A minipage is
--- placed exactly where it is written and never floats, so an image can never
--- drift out of its section or onto the footer. When a page break falls inside a
--- row the whole row moves together to the next page (a clean break, not a gap).
---
--- Pairing: an image is put in a row with the paragraph that immediately follows
--- it (the detail paragraph), or, failing that, the paragraph immediately before
--- it (the intro sentence). The very first image is the wide hero DAG, which is
--- centred full width instead. An image with no adjacent paragraph (e.g. one that
--- sits alone before a heading or a list) is centred, capped at half width.
---
--- Sizes are emitted explicitly (`max width` / `max totalheight`, from adjustbox's
--- [export] option); the `\setkeys{Gin}{...}` default in pdf.yaml is silently
--- ignored by graphicx for bare \includegraphics on some TeX Live builds, which is
--- what let screenshots overflow the page — so we never rely on it. max-* only
--- ever shrinks, so genuinely small captures keep their natural size.

-- The right (image) column of a paired row, pinned flush to the right margin,
-- and the fixed gutter separating it from the text. The left (text) column takes
-- whatever is left over so text + gutter + image exactly fills \linewidth: this
-- keeps the image flush right while guaranteeing the text never crowds it.
local IMAGE_COLUMN_WIDTH = "0.46\\linewidth"
local IMAGE_GUTTER = "2em"
local TEXT_COLUMN_WIDTH = "\\dimexpr\\linewidth-" .. IMAGE_COLUMN_WIDTH .. "-" .. IMAGE_GUTTER .. "\\relax"

-- Cap the image inside its column, and cap its height so a tall portrait capture
-- cannot make a row taller than a page (which would force an ugly early break).
local ROW_IMAGE_KEYS = "max width=\\linewidth, max totalheight=0.34\\textheight"
-- The hero and lone/centred images may use more of the width but stay height-capped.
local HERO_IMAGE_KEYS = "max width=\\linewidth, max totalheight=0.42\\textheight"
local CENTRED_IMAGE_KEYS = "max width=0.5\\linewidth, max totalheight=0.42\\textheight"

--- Return the Image if `block` is a standalone-image paragraph (pandoc's
--- implicit-figure shape), otherwise nil.
local function standalone_image(block)
  if block.t == "Para" and #block.content == 1 and block.content[1].t == "Image" then
    return block.content[1]
  end
  return nil
end

local function raw_latex(text)
  return pandoc.RawBlock("latex", text)
end

--- A small italic caption line, or "" when the image had no alt text.
local function caption_latex(caption)
  if caption == "" then
    return ""
  end
  return "\\par\\vspace{3pt}{\\footnotesize\\itshape " .. caption .. "}"
end

--- The image half of a paired row: a right-hand minipage holding the screenshot.
local function image_column_latex(image, caption)
  return "\\end{minipage}\\hspace{" .. IMAGE_GUTTER .. "}%\n"
    .. "\\begin{minipage}[t]{" .. IMAGE_COLUMN_WIDTH .. "}\\vspace{0pt}\\centering\n"
    .. "\\includegraphics[" .. ROW_IMAGE_KEYS .. "]{" .. image.src .. "}"
    .. caption_latex(caption) .. "\n"
    .. "\\end{minipage}\\par\\medskip"
end

--- Emit a two-column row `[text_paragraph | image]` into `output_blocks`.
--- The text paragraph is inserted unchanged so pandoc renders its inline markup;
--- it is only bracketed by the minipage LaTeX.
local function insert_paired_row(output_blocks, text_paragraph, image, caption)
  output_blocks:insert(raw_latex(
    "\\medskip\\noindent\\begin{minipage}[t]{" .. TEXT_COLUMN_WIDTH .. "}\\vspace{0pt}"))
  output_blocks:insert(text_paragraph)
  output_blocks:insert(raw_latex(image_column_latex(image, caption)))
end

--- Emit a non-floating centred image (used for the hero and for lone images that
--- have no adjacent paragraph to sit beside).
local function insert_centred_image(output_blocks, image, caption, size_keys)
  output_blocks:insert(raw_latex(
    "\\begin{center}\n"
    .. "\\includegraphics[" .. size_keys .. "]{" .. image.src .. "}"
    .. caption_latex(caption) .. "\n"
    .. "\\end{center}"))
end

--- GFM pipe tables carry no column widths, so pandoc's LaTeX writer emits plain
--- `l` columns that never wrap — a wide Description column then runs off the page.
--- Assigning explicit relative widths makes the writer use wrapping paragraph
--- (`p{}`) columns instead.
---
--- We weight each column by how much width its content actually needs:
---   * The longest *word* (unbreakable token, e.g. an env-var name or code label)
---     is a hard floor — prose wraps, but a single long word cannot, so a column
---     must be at least wide enough for it or its text spills into the next column.
---   * The longest *cell* is capped before it counts, so one long Description
---     sentence (which wraps freely) does not starve the short label columns.
--- The per-column weight is the larger of the two, normalised to leave a margin.
---
--- Long words are mostly code tokens (config paths, env-var names) typeset in a
--- monospace font, whose glyphs are wider than the proportional body font a raw
--- character count assumes. We scale the longest-word floor up to compensate;
--- because it competes with the capped cell length via max(), this only bites for
--- genuinely long tokens — the columns that would otherwise overflow.
local CELL_LENGTH_CAP = 30
local MONOSPACE_WORD_WIDTH_FACTOR = 1.3

function Table(table_element)
  local column_count = #table_element.colspecs
  if column_count == 0 then
    return nil
  end

  local longest_word_length = {}
  local longest_cell_length = {}
  for column_index = 1, column_count do
    longest_word_length[column_index] = 0
    longest_cell_length[column_index] = 0
  end

  local function measure_row(row)
    for column_index, cell in ipairs(row.cells) do
      local cell_text = pandoc.utils.stringify(cell.contents)
      if #cell_text > longest_cell_length[column_index] then
        longest_cell_length[column_index] = #cell_text
      end
      for word in cell_text:gmatch("%S+") do
        if #word > longest_word_length[column_index] then
          longest_word_length[column_index] = #word
        end
      end
    end
  end

  for _, row in ipairs(table_element.head.rows) do
    measure_row(row)
  end
  for _, body in ipairs(table_element.bodies) do
    for _, row in ipairs(body.body) do
      measure_row(row)
    end
  end

  local column_weight = {}
  local total_weight = 0
  for column_index = 1, column_count do
    local capped_cell_length = math.min(longest_cell_length[column_index], CELL_LENGTH_CAP)
    local word_floor = longest_word_length[column_index] * MONOSPACE_WORD_WIDTH_FACTOR
    -- At least as wide as the longest unbreakable word; floored so an empty
    -- column still gets a usable sliver.
    column_weight[column_index] = math.max(word_floor, capped_cell_length, 3)
    total_weight = total_weight + column_weight[column_index]
  end

  -- Fill 94% of the line width; the remainder covers inter-column padding so the
  -- table never spills past the right margin.
  local usable_fraction = 0.94
  for column_index, colspec in ipairs(table_element.colspecs) do
    colspec[2] = usable_fraction * column_weight[column_index] / total_weight
  end

  return table_element
end

function Pandoc(doc)
  local blocks = doc.blocks
  local output_blocks = pandoc.List()
  local hero_done = false
  local i = 1
  while i <= #blocks do
    local image = standalone_image(blocks[i])
    if not image then
      output_blocks:insert(blocks[i])
      i = i + 1
    elseif not hero_done then
      -- The first screenshot is the wide hero DAG: centre it full width.
      hero_done = true
      insert_centred_image(output_blocks, image, pandoc.utils.stringify(image.caption), HERO_IMAGE_KEYS)
      i = i + 1
    else
      local caption = pandoc.utils.stringify(image.caption)
      local following_block = blocks[i + 1]
      local preceding_block = output_blocks[#output_blocks]
      if following_block ~= nil and following_block.t == "Para" then
        -- Pair with the detail paragraph that follows the image.
        insert_paired_row(output_blocks, following_block, image, caption)
        i = i + 2
      elseif preceding_block ~= nil and preceding_block.t == "Para" then
        -- No following paragraph: pair with the intro sentence just above.
        output_blocks:remove(#output_blocks)
        insert_paired_row(output_blocks, preceding_block, image, caption)
        i = i + 1
      else
        -- Lone image with no text to sit beside: centre it at half width.
        insert_centred_image(output_blocks, image, caption, CENTRED_IMAGE_KEYS)
        i = i + 1
      end
    end
  end
  return pandoc.Pandoc(output_blocks, doc.meta)
end
