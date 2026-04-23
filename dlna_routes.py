#!/usr/bin/env python3
"""
dlna_routes.py — HTTP path → handler-function route tables.

Separated from dlna_server.py so the router stays thin and adding an
endpoint doesn't grow the server module. Each handler receives
`(handler_instance, params_or_body)` — GET handlers take the parsed
query-string dict, POST handlers take the raw request body.

Adding a new endpoint:
    1. Define the handler function in the relevant api_* module.
    2. Register its path below.
"""
import api_browse
import api_playback
import api_playlists
import api_upnp


GET_ROUTES = {
    "/api/servers":        api_browse.servers,
    "/api/renderers":      api_browse.renderers,
    "/api/browse":         api_browse.browse,
    "/api/artists":        api_browse.artists,
    "/api/search":         api_browse.search,
    "/api/album_tracks":   api_browse.album_tracks,
    "/api/albums":         api_browse.albums,
    "/api/genres":         api_browse.genres,
    "/api/genre_albums":   api_browse.genre_albums,
    "/api/genre_tracks":   api_browse.genre_tracks,
    "/api/artist_albums":  api_browse.artist_albums,
    "/api/browse_letter":  api_browse.browse_letter,
    "/api/renderer_state": api_playback.renderer_state,
    "/api/index/status":   api_playback.index_status,
    "/api/index/rebuild":  api_playback.index_rebuild,
    "/stream":             api_playback.stream,
    "/art":                api_playback.art,
    "/api/playlists":      api_playlists.playlists,
    "/api/playlist":       api_playlists.playlist,
    "/api/playlist/create":api_playlists.playlist_create,
    "/api/playlist/delete":api_playlists.playlist_delete,
    "/api/playlist/add":   api_playlists.playlist_add,
    "/api/playlist/remove":api_playlists.playlist_remove,
    "/gw/device.xml":      api_upnp.device_xml,
    "/gw/cd/desc.xml":     api_upnp.cd_desc_xml,
    "/gw/cd/events":       api_upnp.cd_events,
}

POST_ROUTES = {
    "/api/render_queue": api_playback.render_queue,
    "/api/render":       api_playback.render,
    "/api/control":      api_playback.control,
    "/api/edit_track":   api_playback.edit_track,
    "/api/client_log":   api_playback.client_log,
    "/gw/cd/control":    api_upnp.cd_control,
}
