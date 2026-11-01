import type { ReactNode } from "react";

import type { Profile } from "../types";

export default function StructureView({ profile }: { profile: Profile }) {
  return (
    <div className="structure">
      {profile.theme && <Theme theme={profile.theme} />}
      {profile.format === "docx" && <Docx profile={profile} />}
      {profile.format === "pptx" && <Pptx profile={profile} />}
      {profile.format === "xlsx" && <Xlsx profile={profile} />}
    </div>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="structure-section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function Theme({ theme }: { theme: Profile }) {
  const colors = Object.entries((theme.colors ?? {}) as Record<string, string>);
  return (
    <Section title={`Theme${theme.name ? `: ${theme.name}` : ""}`}>
      <p>
        Headings in {theme.major_font ?? "the default font"}, body in {theme.minor_font ?? "the default font"}.
      </p>
      {colors.length > 0 && (
        <ul className="swatches">
          {colors.map(([name, value]) => (
            <li key={name} title={`${name} ${value}`}>
              <span className="swatch" style={{ background: value }} />
              <span>{name}</span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <div className="structure-table">
      <table>
        <thead>
          <tr>
            {head.map((title) => (
              <th key={title}>{title}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, r) => (
            <tr key={r}>
              {row.map((cell, c) => (
                <td key={c}>{cell ?? "-"}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Docx({ profile }: { profile: Profile }) {
  return (
    <>
      <Section title="Page">
        <Table
          head={["Section", "Size (mm)", "Orientation", "Margins T/R/B/L (mm)", "Header / footer (mm)"]}
          rows={profile.page_setup.map((page: Profile, index: number) => [
            index + 1,
            `${page.width_mm} x ${page.height_mm}`,
            page.orientation,
            `${page.margins_mm.top} / ${page.margins_mm.right} / ${page.margins_mm.bottom} / ${page.margins_mm.left}`,
            `${page.margins_mm.header} / ${page.margins_mm.footer}`,
          ])}
        />
      </Section>
      <Section title="Styles in use">
        <Table
          head={["Style", "Paragraphs", "Font", "Size", "Colour", "Before / after"]}
          rows={profile.styles_in_use.map((style: Profile) => [
            style.name,
            style.paragraphs,
            style.font,
            style.size_pt ? `${style.size_pt} pt` : null,
            style.color ? <ColorValue value={style.color} /> : null,
            style.space_before_pt != null || style.space_after_pt != null
              ? `${style.space_before_pt ?? 0} / ${style.space_after_pt ?? 0} pt`
              : null,
          ])}
        />
      </Section>
      {profile.images.length > 0 && (
        <Section title="Images and logos">
          <Table
            head={["Id", "In", "Placement", "Size (mm)", "Position"]}
            rows={profile.images.map((image: Profile) => [
              image.id,
              image.location,
              image.mode,
              `${image.width_mm} x ${image.height_mm}`,
              image.mode === "anchored"
                ? `${image.x_mm ?? image.x_align} from ${image.x_relative_to}, ${image.y_mm ?? image.y_align} from ${image.y_relative_to}`
                : "in the text",
            ])}
          />
        </Section>
      )}
      {profile.tables.length > 0 && (
        <Section title="Tables">
          <Table
            head={["Id", "Size", "Header fill", "Borders", "Merged"]}
            rows={profile.tables.map((table: Profile) => [
              table.id,
              `${table.rows} x ${table.columns}`,
              table.header_fill ? <ColorValue value={table.header_fill} /> : null,
              Object.keys(table.borders).join(", ") || table.style,
              table.has_merged_cells ? "yes" : "no",
            ])}
          />
        </Section>
      )}
      {profile.headers_footers.length > 0 && (
        <Section title="Headers and footers">
          <Table
            head={["Where", "Text", "Images", "Page number"]}
            rows={profile.headers_footers.map((item: Profile) => [
              item.kind,
              item.text,
              item.images,
              item.has_page_number ? "yes" : "no",
            ])}
          />
        </Section>
      )}
      {profile.headings.length > 0 && (
        <Section title="Outline">
          <ul className="outline">
            {profile.headings.map((heading: Profile) => (
              <li key={heading.id} style={{ paddingLeft: `${heading.level * 0.9}rem` }}>
                {heading.text}
              </li>
            ))}
          </ul>
        </Section>
      )}
    </>
  );
}

function Pptx({ profile }: { profile: Profile }) {
  return (
    <>
      <Section title="Slides">
        <p>
          {profile.stats.slides} slides at {profile.slide_size_mm.width} x {profile.slide_size_mm.height} mm.
        </p>
        <Table
          head={["Slide", "Layout", "Shapes"]}
          rows={profile.slides.map((slide: Profile, index: number) => [
            index + 1,
            slide.layout,
            slide.shapes
              .map((shape: Profile) => shape.kind)
              .reduce((counts: Record<string, number>, kind: string) => ({ ...counts, [kind]: (counts[kind] ?? 0) + 1 }), {}) &&
              summarise(slide.shapes.map((shape: Profile) => shape.kind)),
          ])}
        />
      </Section>
      {profile.masters.map((master: Profile, index: number) => (
        <Section key={index} title={`Slide master: ${master.name}`}>
          <p>{summarise(master.shapes.map((shape: Profile) => shape.kind))}</p>
          <Table
            head={["Layout", "Placeholders"]}
            rows={master.layouts.map((layout: Profile) => [
              layout.name,
              layout.placeholders.map((ph: Profile) => ph.type).join(", "),
            ])}
          />
        </Section>
      ))}
    </>
  );
}

function summarise(kinds: string[]): string {
  const counts = new Map<string, number>();
  for (const kind of kinds) counts.set(kind, (counts.get(kind) ?? 0) + 1);
  return [...counts].map(([kind, count]) => `${count} ${kind}${count > 1 ? "s" : ""}`).join(", ") || "none";
}

function Xlsx({ profile }: { profile: Profile }) {
  const fills = Object.keys(profile.fills ?? {});
  return (
    <>
      <Section title="Workbook">
        <p>
          {profile.stats.sheets} sheets, {profile.stats.formula_cells} formula cells, {profile.objects.charts} charts,{" "}
          {profile.objects.images} images, {profile.objects.pivot_tables} pivot tables.
          {profile.named_ranges.length > 0 && ` Named ranges: ${profile.named_ranges.join(", ")}.`}
        </p>
        {fills.length > 0 && (
          <ul className="swatches">
            {fills.map((value) => (
              <li key={value} title={value}>
                <span className="swatch" style={{ background: value }} />
                <span>{value}</span>
              </li>
            ))}
          </ul>
        )}
        <p className="structure-small">Fonts: {Object.keys(profile.fonts ?? {}).join(", ") || "defaults only"}.</p>
      </Section>
      {profile.sheets.map((sheet: Profile) => (
        <Section key={sheet.name} title={`Sheet: ${sheet.name}`}>
          <dl className="facts">
            <Fact label="Used range" value={sheet.dimensions} />
            <Fact label="Frozen at" value={sheet.freeze_panes} />
            <Fact label="Formulas" value={sheet.formula_cells} />
            <Fact label="Merged" value={sheet.merged_cells.join(", ")} />
            <Fact label="Conditional formats" value={sheet.conditional_formats.join(", ")} />
            <Fact label="Data validation" value={sheet.data_validations.join(", ")} />
            <Fact label="Number formats" value={Object.keys(sheet.number_formats).join("  ")} />
            <Fact label="Page" value={sheet.page_setup.orientation} />
            <Fact label="Print header" value={sheet.print_header} />
            <Fact label="Print footer" value={sheet.print_footer} />
          </dl>
        </Section>
      ))}
    </>
  );
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  if (value === null || value === undefined || value === "" || value === 0) return null;
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function ColorValue({ value }: { value: string }) {
  return (
    <span className="color-value">
      <span className="swatch swatch-small" style={{ background: value }} />
      {value}
    </span>
  );
}
