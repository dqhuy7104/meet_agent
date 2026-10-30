import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { AuthProvider } from "./lib/AuthProvider";
import { AuthPages } from "./components/AuthPages";
import "./styles.css";
import "./light.css";
import "./geometry.css";
import "./dark.css";
import "./access.css";

const isAuthPage = ["/login", "/signup"].includes(window.location.pathname);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {isAuthPage ? (
      <BrowserRouter>
        <AuthProvider>
          <AuthPages />
        </AuthProvider>
      </BrowserRouter>
    ) : (
      <BrowserRouter basename="/app">
        <App />
      </BrowserRouter>
    )}
  </StrictMode>,
);
