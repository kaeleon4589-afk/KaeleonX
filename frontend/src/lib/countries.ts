export type CountryOption = { iso: string; name: string; dial: string };

export const countries: CountryOption[] = [
  {iso:'MX',name:'México',dial:'+52'},{iso:'CU',name:'Cuba',dial:'+53'},{iso:'US',name:'Estados Unidos',dial:'+1'},{iso:'CA',name:'Canadá',dial:'+1'},
  {iso:'AR',name:'Argentina',dial:'+54'},{iso:'BO',name:'Bolivia',dial:'+591'},{iso:'BR',name:'Brasil',dial:'+55'},{iso:'CL',name:'Chile',dial:'+56'},
  {iso:'CO',name:'Colombia',dial:'+57'},{iso:'CR',name:'Costa Rica',dial:'+506'},{iso:'DO',name:'República Dominicana',dial:'+1'},{iso:'EC',name:'Ecuador',dial:'+593'},
  {iso:'SV',name:'El Salvador',dial:'+503'},{iso:'GT',name:'Guatemala',dial:'+502'},{iso:'HN',name:'Honduras',dial:'+504'},{iso:'NI',name:'Nicaragua',dial:'+505'},
  {iso:'PA',name:'Panamá',dial:'+507'},{iso:'PY',name:'Paraguay',dial:'+595'},{iso:'PE',name:'Perú',dial:'+51'},{iso:'PR',name:'Puerto Rico',dial:'+1'},
  {iso:'UY',name:'Uruguay',dial:'+598'},{iso:'VE',name:'Venezuela',dial:'+58'},{iso:'ES',name:'España',dial:'+34'},{iso:'PT',name:'Portugal',dial:'+351'},
  {iso:'GB',name:'Reino Unido',dial:'+44'},{iso:'FR',name:'Francia',dial:'+33'},{iso:'DE',name:'Alemania',dial:'+49'},{iso:'IT',name:'Italia',dial:'+39'},
  {iso:'NL',name:'Países Bajos',dial:'+31'},{iso:'BE',name:'Bélgica',dial:'+32'},{iso:'CH',name:'Suiza',dial:'+41'},{iso:'AT',name:'Austria',dial:'+43'},
  {iso:'IE',name:'Irlanda',dial:'+353'},{iso:'DK',name:'Dinamarca',dial:'+45'},{iso:'SE',name:'Suecia',dial:'+46'},{iso:'NO',name:'Noruega',dial:'+47'},
  {iso:'FI',name:'Finlandia',dial:'+358'},{iso:'PL',name:'Polonia',dial:'+48'},{iso:'CZ',name:'Chequia',dial:'+420'},{iso:'GR',name:'Grecia',dial:'+30'},
  {iso:'RO',name:'Rumanía',dial:'+40'},{iso:'HU',name:'Hungría',dial:'+36'},{iso:'UA',name:'Ucrania',dial:'+380'},{iso:'TR',name:'Turquía',dial:'+90'},
  {iso:'RU',name:'Rusia',dial:'+7'},{iso:'IL',name:'Israel',dial:'+972'},{iso:'AE',name:'Emiratos Árabes Unidos',dial:'+971'},{iso:'SA',name:'Arabia Saudita',dial:'+966'},
  {iso:'IN',name:'India',dial:'+91'},{iso:'PK',name:'Pakistán',dial:'+92'},{iso:'BD',name:'Bangladés',dial:'+880'},{iso:'LK',name:'Sri Lanka',dial:'+94'},
  {iso:'CN',name:'China',dial:'+86'},{iso:'JP',name:'Japón',dial:'+81'},{iso:'KR',name:'Corea del Sur',dial:'+82'},{iso:'HK',name:'Hong Kong',dial:'+852'},
  {iso:'TW',name:'Taiwán',dial:'+886'},{iso:'SG',name:'Singapur',dial:'+65'},{iso:'MY',name:'Malasia',dial:'+60'},{iso:'TH',name:'Tailandia',dial:'+66'},
  {iso:'VN',name:'Vietnam',dial:'+84'},{iso:'ID',name:'Indonesia',dial:'+62'},{iso:'PH',name:'Filipinas',dial:'+63'},{iso:'AU',name:'Australia',dial:'+61'},
  {iso:'NZ',name:'Nueva Zelanda',dial:'+64'},{iso:'ZA',name:'Sudáfrica',dial:'+27'},{iso:'EG',name:'Egipto',dial:'+20'},{iso:'MA',name:'Marruecos',dial:'+212'},
  {iso:'DZ',name:'Argelia',dial:'+213'},{iso:'TN',name:'Túnez',dial:'+216'},{iso:'NG',name:'Nigeria',dial:'+234'},{iso:'GH',name:'Ghana',dial:'+233'},
  {iso:'KE',name:'Kenia',dial:'+254'},{iso:'ET',name:'Etiopía',dial:'+251'},{iso:'TZ',name:'Tanzania',dial:'+255'},{iso:'UG',name:'Uganda',dial:'+256'},
  {iso:'JM',name:'Jamaica',dial:'+1'},{iso:'TT',name:'Trinidad y Tobago',dial:'+1'},{iso:'BS',name:'Bahamas',dial:'+1'},{iso:'BB',name:'Barbados',dial:'+1'},
  {iso:'BZ',name:'Belice',dial:'+501'},{iso:'GY',name:'Guyana',dial:'+592'},{iso:'SR',name:'Surinam',dial:'+597'},{iso:'HT',name:'Haití',dial:'+509'},
  {iso:'IS',name:'Islandia',dial:'+354'},{iso:'LU',name:'Luxemburgo',dial:'+352'},{iso:'MT',name:'Malta',dial:'+356'},{iso:'CY',name:'Chipre',dial:'+357'},
  {iso:'BG',name:'Bulgaria',dial:'+359'},{iso:'HR',name:'Croacia',dial:'+385'},{iso:'RS',name:'Serbia',dial:'+381'},{iso:'SI',name:'Eslovenia',dial:'+386'},
  {iso:'SK',name:'Eslovaquia',dial:'+421'},{iso:'LT',name:'Lituania',dial:'+370'},{iso:'LV',name:'Letonia',dial:'+371'},{iso:'EE',name:'Estonia',dial:'+372'},
  {iso:'GE',name:'Georgia',dial:'+995'},{iso:'AM',name:'Armenia',dial:'+374'},{iso:'AZ',name:'Azerbaiyán',dial:'+994'},{iso:'KZ',name:'Kazajistán',dial:'+7'}
];

export function flagEmoji(iso:string){
  return iso.toUpperCase().replace(/./g, ch => String.fromCodePoint(127397 + ch.charCodeAt(0)));
}
