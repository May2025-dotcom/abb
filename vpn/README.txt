VPN settings for the gluetun container (NordVPN over OpenVPN).

1. Copy vpn.env.example to vpn.env  (Windows: copy vpn.env.example vpn.env)
2. Fill in your NordVPN *service credentials*:
   NordVPN website -> Dashboard -> NordVPN -> "Set up NordVPN manually"
   -> Service credentials. These are NOT your NordVPN email and password.
3. Optionally change SERVER_COUNTRIES (e.g. Netherlands) if AudioBook Bay
   blocks the servers you get.

Keep vpn.env private: it contains your VPN login. It is ignored by git.

Don't use the same login in another OpenVPN client at the same time as the
container, or the connections may drop on and off.
