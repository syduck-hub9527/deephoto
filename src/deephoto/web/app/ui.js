import { h, createContext } from 'preact';
import { useContext } from 'preact/hooks';
import htm from 'htm';
export const html = htm.bind(h);
export const AppContext = createContext(null);
export const useApp = () => useContext(AppContext);
