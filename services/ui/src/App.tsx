/** Корневой компонент: оболочка приложения, навигация, баннер доступности API, маршруты. */

import { useEffect, useState } from "react";
import type { ComponentType } from "react";
import { Link, Route, Routes, useLocation } from "react-router-dom";
import { api } from "./api/client";
import { Banner } from "./components/ui";
import { Logo } from "./components/Logo";
import { IconBook, IconModel, IconSearch, IconSignal } from "./components/icons";
import { cssVars } from "./lib/style";
import WelcomePage from "./pages/WelcomePage";
import QueryPage from "./pages/QueryPage";
import ResultsPage from "./pages/ResultsPage";
import InsightPage from "./pages/InsightPage";
import ModelPage from "./pages/ModelPage";
import MethodologyPage from "./pages/MethodologyPage";

const READINESS_INTERVAL_MS = 5000;

const NAV: { to: string; label: string; Icon: ComponentType<{ size?: number }>; prefixes: string[] }[] = [
  { to: "/", label: "Обзор", Icon: IconSignal, prefixes: [] },
  { to: "/search", label: "Запрос", Icon: IconSearch, prefixes: ["/search", "/results", "/insight"] },
  { to: "/model", label: "Модель", Icon: IconModel, prefixes: ["/model"] },
  { to: "/methodology", label: "Методология", Icon: IconBook, prefixes: ["/methodology"] },
];

function activeIndex(pathname: string): number {
  if (pathname === "/") return 0;
  return NAV.findIndex((entry) => entry.prefixes.some((prefix) => pathname.startsWith(prefix)));
}

export default function App() {
  const [apiReady, setApiReady] = useState(true);
  const location = useLocation();
  const current = activeIndex(location.pathname);

  useEffect(() => {
    let active = true;
    const check = async () => {
      const ready = await api.readiness();
      if (active) setApiReady(ready);
    };
    void check();
    const timer = setInterval(check, READINESS_INTERVAL_MS);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    window.scrollTo(0, 0);
  }, [location.pathname]);

  return (
    <div className="app">
      <header className="chrome">
        <Link className="brand" to="/" aria-label="Слабые сигналы — на главную">
          <span className="brand__mark">
            <Logo size={30} title="" />
          </span>
          <span className="brand__text">
            Слабые сигналы
            <span className="brand__sub">поиск зарождающихся технологий</span>
          </span>
        </Link>

        <nav className="nav" aria-label="Основная навигация" style={cssVars({ "--nav-index": Math.max(current, 0) })}>
          {current >= 0 ? <span className="nav__indicator" aria-hidden="true" /> : null}
          {NAV.map(({ to, label, Icon }, index) => (
            <Link
              key={to}
              to={to}
              className={`nav__link ${index === current ? "nav__link--active" : ""}`}
              aria-current={index === current ? "page" : undefined}
            >
              <Icon size={20} />
              <span className="nav__label">{label}</span>
            </Link>
          ))}
        </nav>

        <div className={`api-state ${apiReady ? "api-state--ok" : "api-state--down"}`} role="status">
          <span className="api-state__dot" aria-hidden="true" />
          <span className="api-state__text">{apiReady ? "API на связи" : "API недоступен"}</span>
        </div>
      </header>

      <main className="main" id="main">
        {!apiReady ? (
          <div className="main__banner">
            <Banner kind="error" title="API недоступен">
              Сервис orchestrator-api не отвечает. Проверьте, что контейнеры запущены
              (<code>docker compose ps</code>). Страница продолжит проверять доступность каждые 5 секунд.
            </Banner>
          </div>
        ) : null}
        <div className="view" key={location.pathname}>
          <Routes>
            <Route path="/" element={<WelcomePage />} />
            <Route path="/search" element={<QueryPage />} />
            <Route path="/results/:jobId" element={<ResultsPage />} />
            <Route path="/insight/:itemId" element={<InsightPage />} />
            <Route path="/model" element={<ModelPage />} />
            <Route path="/methodology" element={<MethodologyPage />} />
            <Route
              path="*"
              element={
                <div className="page page--narrow">
                  <Banner kind="warn" title="Страница не найдена">
                    Проверьте адрес или вернитесь на страницу запроса.
                  </Banner>
                </div>
              }
            />
          </Routes>
        </div>
        <footer className="footer">
          Аналитическая панель слабых сигналов. Вся выдача формируется по открытым источникам и
          сопровождается объяснением решения модели.
        </footer>
      </main>
    </div>
  );
}
