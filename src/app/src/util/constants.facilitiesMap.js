import env from './env';

export const GOOGLE_CLIENT_SIDE_API_KEY = env('REACT_APP_GOOGLE_CLIENT_SIDE_API_KEY');

export const initialCenter = Object.freeze({
    lat: 34,
    lng: 5,
});

export const initialZoom = 1;
