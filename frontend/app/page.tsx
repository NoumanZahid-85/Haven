"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

const REPO = "https://github.com/NoumanZahid-85/voicesell-ai";

/** The visitor's own clock: the product exists for whatever time it is right now. */
function useLocalTime() {
  const [time, setTime] = useState("");
  useEffect(() => {
    const tick = () =>
      setTime(new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).toLowerCase());
    tick();
    const id = setInterval(tick, 15_000);
    return () => clearInterval(id);
  }, []);
  return time;
}

export default function Landing() {
  const time = useLocalTime();

  return (
    <>
      <header className="landing-bar">
        <span className="wordmark">Haven</span>
        <Link href="/talk" className="btn-quiet btn-sm">
          Start talking
        </Link>
      </header>

      <main>
        <section className="hero">
          <div>
            <p className="hero-time" aria-hidden={!time}>
              {time ? `It's ${time}.` : " "}
            </p>
            <h1 className="hero-title">Someone to talk to, right now.</h1>
            <p className="hero-copy">
              A night you can&apos;t sleep. Dread before work. A breakup you keep replaying. Haven is a voice companion you
              can talk to out loud, at any hour. It listens first, remembers what matters to you, and knows when to point
              you to a person.
            </p>
            <div className="hero-actions">
              <Link href="/talk" className="btn-primary">
                Start talking
              </Link>
              <a href="#how" className="btn-quiet">
                How it works
              </a>
            </div>
          </div>
          <div className="hero-lamp" aria-hidden="true" />
        </section>

        <section id="how" className="prose-section">
          <h2>What happens when you talk</h2>
          <div className="body">
            <p>
              Tap the light and say what&apos;s on your mind. Haven usually starts answering within about two seconds of
              you finishing, in a calm voice, a sentence or two at a time. You can talk over it whenever you like: a
              quick &ldquo;mm-hmm&rdquo; won&apos;t stop it, but &ldquo;wait&hellip;&rdquo; will, in under a second.
            </p>
            <p>
              When it fits, Haven offers one small thing to try, like a breathing pattern, a way to quiet racing
              thoughts at bedtime, or a different way to look at a harsh thought. It draws on fifteen techniques from
              CBT, ACT and sleep research, and only suggests one when you seem open to it.
            </p>
            <p>
              Between conversations it keeps a few short notes, such as who your sister is or that the breathing
              exercise helped last time, so you don&apos;t have to start over. You can read every note and erase them all
              on the <strong>Memory</strong> page.
            </p>
          </div>
        </section>

        <section className="prose-section">
          <h2>Two ways to talk</h2>
          <div className="body">
            <div className="modes-compare">
              <div>
                <h3>Natural</h3>
                <p>
                  Haven hears your voice and answers with its own in a single step, so pauses, tone and timing feel the
                  most human. This is the fastest mode.
                </p>
                <p className="stack-line">Gemini Live native speech-to-speech</p>
              </div>
              <div>
                <h3>Checked</h3>
                <p>
                  Haven writes down what you said, thinks, then speaks one sentence at a time, and every sentence is
                  screened before you hear it. A little slower, and the most careful.
                </p>
                <p className="stack-line">Deepgram Flux, then Groq, then Deepgram Aura-2</p>
              </div>
            </div>
            <p>
              Both modes speak <strong>English and German</strong>. For German, Natural understands you best.
            </p>
          </div>
        </section>

        <section className="prose-section">
          <h2>When nothing feels like enough</h2>
          <div className="body">
            <p>
              Some of the people awake at 2 a.m. look fine from the outside. They hit the target, closed the deal, and
              felt nothing. Haven listens for the wins you skip past, keeps them, and brings them back when you need them.
              Every one is on your <strong>Journey</strong> page.
            </p>
            <p>
              When you hang up, Haven leaves you a short takeaway: what you talked through, how your mood moved, and one
              small step you can take by tomorrow, not two weeks from now.
            </p>
          </div>
        </section>

        <section className="prose-section">
          <h2>When it&apos;s serious</h2>
          <div className="body">
            <p>
              If something you say suggests you might be in danger, Haven doesn&apos;t improvise. It stops the AI, speaks
              a fixed message with real helplines, and keeps those numbers on screen for the rest of the conversation.
            </p>
            <ul className="help-lines">
              <li>United States: call or text 988</li>
              <li>UK and Ireland: Samaritans, 116 123</li>
              <li>
                Anywhere else:{" "}
                <a href="https://findahelpline.com" target="_blank" rel="noreferrer">
                  findahelpline.com
                </a>
              </li>
            </ul>
            <p>Haven is not a therapist, and it is not an emergency service.</p>
          </div>
        </section>

        <section className="prose-section">
          <h2>Under the hood</h2>
          <div className="body">
            <p>
              One WebSocket carries your voice to the server and the reply back as streamed audio. A safety layer reads
              the conversation in both directions, every reply is stored with its timing, and an AI reviewer scores
              conversations so the weakest ones become tests for the next version. The live numbers are on the{" "}
              <Link href="/insights">Insights</Link> page, and the code is on <a href={REPO}>GitHub</a>.
            </p>
          </div>
        </section>
      </main>

      <footer className="landing-footer">
        <span>Built by Nouman Zahid</span>
        <a href={REPO}>Source code</a>
        <Link href="/insights">Insights</Link>
      </footer>
    </>
  );
}
