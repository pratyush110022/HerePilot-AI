import { hasTripadvisorData } from "../format";
import type { AnswerBlock, AnswerCard, PlaceItem } from "../types";
import { PlaceRow, TripadvisorCredit, type ShowHandler } from "./PlaceRow";

function PlaceList({ items, onShow, label }: { items: PlaceItem[]; onShow: ShowHandler; label: string }) {
  if (!items.length) return null;
  // No numbers: the answer's wording never refers to a place by position, and nothing here is a ranking.
  return <ul className="place-list answer-places" aria-label={label}>{items.map((item, i) => <PlaceRow key={`${item.key}-${i}`} item={item} onShow={onShow} />)}</ul>;
}

function Heading({ title }: { title: string }) {
  return title ? <h4 className="answer-heading">{title}</h4> : null;
}

function Block({ block, onShow }: { block: AnswerBlock; onShow: ShowHandler }) {
  switch (block.type) {
    case "text":
      return <div className="answer-block"><Heading title={block.title} /><p className="answer-text">{block.text}</p></div>;
    case "list":
      return <div className="answer-block"><Heading title={block.title} /><ul className="answer-list">{block.items.map((item) => <li key={item}>{item}</li>)}</ul></div>;
    case "steps":
      return <div className="answer-block"><Heading title={block.title} /><ol className="answer-steps">{block.items.map((item) => <li key={item}>{item}</li>)}</ol></div>;
    case "pros_cons":
      return (
        <div className="answer-block">
          <Heading title={block.title} />
          <div className="proscons">
            {block.pros.length > 0 && (
              <section className="pc pc-pros" aria-label="Advantages">
                <h5><span aria-hidden="true">＋ </span>Pros</h5>
                <ul>{block.pros.map((p) => <li key={p}>{p}</li>)}</ul>
              </section>
            )}
            {block.cons.length > 0 && (
              <section className="pc pc-cons" aria-label="Disadvantages">
                <h5><span aria-hidden="true">－ </span>Cons</h5>
                <ul>{block.cons.map((c) => <li key={c}>{c}</li>)}</ul>
              </section>
            )}
          </div>
        </div>
      );
    case "compare":
      return (
        <div className="answer-block">
          <Heading title={block.title} />
          <div className="compare-wrap" tabIndex={0} role="region" aria-label={block.title || "Comparison"}>
            <table className="compare">
              <thead>
                <tr><th scope="col"><span className="sr-only">Aspect</span></th>{block.columns.map((c) => <th key={c} scope="col">{c}</th>)}</tr>
              </thead>
              <tbody>
                {block.rows.map((row) => (
                  <tr key={row.label}><th scope="row">{row.label}</th>{block.columns.map((c, i) => <td key={c}>{row.values[i] ?? "—"}</td>)}</tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      );
    case "itinerary":
      return (
        <div className="answer-block">
          <Heading title={block.title} />
          <div className="itinerary">
            {block.days.map((day) => (
              <section key={day.title} className="day">
                <h5>{day.title}</h5>
                <ul>{day.items.map((item) => <li key={item}>{item}</li>)}</ul>
              </section>
            ))}
          </div>
        </div>
      );
    case "facts":
      return (
        <div className="answer-block">
          <Heading title={block.title} />
          <dl className="facts">{block.items.map((f) => <div key={f.label}><dt>{f.label}</dt><dd>{f.value}</dd></div>)}</dl>
        </div>
      );
    case "places":
      return <div className="answer-block"><Heading title={block.title} /><PlaceList items={block.items} onShow={onShow} label={block.title || "Places"} /></div>;
    default:
      return null;
  }
}

/** The details behind an answer: the places it is about, the blocks the AI chose, what it is unsure of, and its sources. */
export default function AnswerCardView({ card, onShow }: { card: AnswerCard; onShow: ShowHandler }) {
  const placed = [...card.subjects, ...card.blocks.flatMap((b) => (b.type === "places" ? b.items : [])), ...card.more];
  const empty = !card.subjects.length && !card.blocks.length && !card.more.length && !card.caveats.length;
  if (empty && !card.note) return null;
  return (
    <section className="card answer" aria-label="Answer details">
      {card.note && <p className="card-state card-state-info" role="status">{card.note}</p>}
      <PlaceList items={card.subjects} onShow={onShow} label="The place you asked about" />
      {card.blocks.map((block, i) => <Block key={i} block={block} onShow={onShow} />)}
      {card.more.length > 0 && (
        <div className="answer-block"><Heading title="Places HERE lists" /><PlaceList items={card.more} onShow={onShow} label="Places HERE lists" /></div>
      )}
      {card.status === "unavailable" && <p className="card-state card-state-warn" role="status">Place search is unavailable right now, so no places are shown.</p>}
      {card.caveats.length > 0 && (
        <aside className="answer-caveats" aria-label="Things to keep in mind">
          <h4 className="answer-heading">Keep in mind</h4>
          <ul>{card.caveats.map((c) => <li key={c}>{c}</li>)}</ul>
        </aside>
      )}
      <footer className="answer-sources">
        <ul className="source-chips" aria-label="Where this comes from">
          {card.ai && <li className="source-chip source-ai">AI: general knowledge, not live data</li>}
          {placed.length > 0 && <li className="source-chip source-here">Places and hours: HERE</li>}
        </ul>
        {hasTripadvisorData(placed) && <TripadvisorCredit />}
      </footer>
    </section>
  );
}
