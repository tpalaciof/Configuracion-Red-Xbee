#!/usr/bin/env python3
"""
Configurador local de una red XBee-PRO 900HP DigiMesh sin XCTU.

El programa se comunica con un XBee conectado a la Raspberry Pi mediante
un adaptador CP2102 USB-UART. Permite:

1. Leer la configuración actual del XBee local.
2. Configurar ID, AP, CE y NI.
3. Guardar los cambios en la memoria no volátil mediante WR.
4. Verificar que los valores fueron aplicados correctamente.
5. Descubrir los XBee que ya pertenecen a la misma red mediante ND.
6. Guardar un registro local de los módulos configurados.

Organización de la comunicación:

    Lectura, configuración y verificación local: +++ y comandos AT de texto.
    Esto funciona con AP = 0, 1 o 2; entrar en modo comando no cambia AP.
    API se utiliza únicamente para ND y sus múltiples respuestas 0x88.
    NO y NT también se consultan por texto antes de iniciar ND.

No se configura ningún XBee remoto: 0x08 ejecuta ND en el XBee local.
No se implementan comandos remotos 0x17/0x97 ni recepción de sensores.
Se conserva el descubrimiento API que ya funcionaba en este proyecto;
ATND por texto sería otra implementación posible, no incluida aquí.

Referencia del protocolo: manual Digi 90002173, firmware 900HP.
https://docs.digi.com/resources/documentation/digidocs/pdfs/90002173.pdf

Configuración recomendada para este proyecto:

    Raspberry: AP = 1, CE = 1  (Indirect Msg Coordinator)
    Sensores:  AP = 0, CE = 0  (Standard Router)

Requisito:

    python3 -m pip install pyserial
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import serial
    from serial.tools import list_ports
except ModuleNotFoundError as error:
    if error.name == "serial":
        raise SystemExit(
            "Falta pyserial. Instálelo con: python3 -m pip install pyserial"
        ) from error

    raise


# ************************* CONFIGURACIÓN ************************* #

BAUDIOS = 9600
TIEMPO_ESPERA_SERIAL_S = 0.100
TIEMPO_RESPUESTA_AT_S = 2.0
TIEMPO_RESPUESTA_API_S = 2.0
TIEMPO_GUARDA_S = 1.100
TIEMPO_MAXIMO_DESCUBRIMIENTO_S = 60.0

ZONA_HORARIA = ZoneInfo("America/Bogota")

RUTA_REGISTRO = Path(__file__).resolve().with_name(
    "xbee_configurados.json"
)

MODO_COMANDO = "comando"
MODO_API_1 = "api_1"
MODO_API_2 = "api_2"

NOMBRES_AP = {
    0: "Transparent Mode",
    1: "API Mode Without Escapes",
    2: "API Mode With Escapes",
}

NOMBRES_CE = {
    0: "Standard Router",
    1: "Indirect Msg Coordinator",
    2: "Non-Routing Module",
}

NOMBRES_TIPO_NODO = {
    0: "Coordinator",
    1: "Router",
    2: "End Device",
}

NOMBRES_ESTADO_AT = {
    0: "OK",
    1: "ERROR",
    2: "comando no válido",
    3: "parámetro no válido",
    4: "fallo de transmisión",
}

BAUDIOS_POR_BD = {
    0: 1200,
    1: 2400,
    2: 4800,
    3: 9600,
    4: 19200,
    5: 38400,
    6: 57600,
    7: 115200,
    8: 230400,
}

COMANDOS_LECTURA_OBLIGATORIOS = ("ID", "AP", "CE", "NI", "SH", "SL")
COMANDOS_LECTURA_ADICIONALES = ("VR", "BD", "HP", "CM", "DH", "DL")


# **************************** ERRORES ***************************** #

class ErrorXBee(Exception):
    """Error controlado durante la comunicación con el XBee."""


# *********************** FUNCIONES BÁSICAS ********************** #

def fechaHoraActual():
    """Devuelve la fecha de Bogotá como texto, incluyendo segundos y zona horaria."""
    return datetime.now(ZONA_HORARIA).isoformat(timespec="seconds")


def bytesAEntero(datos):
    """Interpreta varios bytes como un entero; por ejemplo, b'\x00\x09' vale 9."""
    if not datos:
        return 0

    return int.from_bytes(datos, byteorder="big")


def descripcionModo(modo):
    """Describe el protocolo utilizado, sin confundir modo comando con AP=0."""
    if modo == MODO_COMANDO:
        return "modo comando AT local (independiente de AP)"

    if modo == MODO_API_1:
        return "API 1, sin escapes"

    return "API 2, con escapes"


def pedirConfirmacion(mensaje):
    """Repite la pregunta hasta recibir sí o no; devuelve True o False."""
    while True:
        respuesta = input(f"{mensaje} [s/n]: ").strip().lower()

        if respuesta in ("s", "si", "sí", "y", "yes"):
            return True

        if respuesta in ("n", "no"):
            return False

        print("Escriba s para sí o n para no.")


# ************************ PUERTO SERIAL ************************** #

def listarPuertosSeriales():
    return sorted(list_ports.comports(), key=lambda puerto: puerto.device) #Ordena los puertos encontrados usando puerto.device como criterio de ordenamiento 
 
"""
list_ports.comports() --> busca los puertos seriales que el sistema operativo detecta. Cada objeto tiene varias propiedades:

    puerto.device
    puerto.description
    puerto.manufacturer

Ejemplo: puerto1

    puerto1.device = "/dev/ttyUSB2"
    puerto1.description = "CP2102"
"""
"""
La función sorted() necesita saber con qué criterio ordenar, como list_ports.comports() devuelve objetos que representan puertos seriales, 
se utiliza cómo criterio de ordenamiento 'key' una función que toma un objeto puerto y devuelve el valor de la propiedad 'device' de ese objeto.

    lambda puerto: puerto.device  --->   def obtenerNombrePuerto(puerto):
                                             return puerto.device
    key=obtenerNombrePuerto

Ejemplo: 

    lambda puerto: puerto.device
    
    puerto = puerto1
    puerto1.device = "/dev/ttyUSB2"
    
    puerto = puerto2
    puerto2.device = "/dev/ttyUSB0"

    por lo tanto las claves son: 

    "/dev/ttyUSB2"
    "/dev/ttyUSB0"

    y sorted() las ordena de la siguiente manera: 

    "/dev/ttyUSB0"
    "/dev/ttyUSB2"

    Sin embargo, sorted() no devuelve la cadena "/dev/ttyUSB0" ni "/dev/ttyUSB2", sino que devuelve los objetos 
    completos puerto1 y puerto2, pero en el orden correcto según la propiedad 'device'. Es decir, devuelve algo así: 

    [puerto2, puerto1]
"""

def seleccionarPuerto(puertoPreferido=None, baudios=BAUDIOS):   # puertoPreferido=None, siginifica que, por defecto, no se le ha indicado qué puerto usar. Si quien llama a la función no me entrega un puerto, asumiré que no hay uno preseleccionado.

    if puertoPreferido:                                         # ¿Me dieron ya un puerto específico?

        """ seleccionarPuerto("/dev/ttyUSB0") 
            puertoPreferido = "/dev/ttyUSB0"
        """

        ni = identificarNiEnPuerto(puertoPreferido, baudios)    # Si sí, 'identificarNiEnPuerto' intenta comunicarse con el XBee conectado a ese puerto y averiguar su parámetro NI.
        return puertoPreferido, ni                              # ("/dev/ttyUSB0", "COORDINADOR") 

    puertos = listarPuertosSeriales()
    puertosIdentificados = []

    print()
    print("Puertos seriales disponibles:")

    if puertos:                                                 # si la lista de puertos no está vacía
        for indice, puerto in enumerate(puertos, start=1):      # start = 1 porque por defecto es 0

            """
            Supongamos que:

            puertos = [objetoPuertoUSB0, objetoPuertoUSB1]

            donde:

            objetoPuertoUSB0.device = "/dev/ttyUSB0"
            objetoPuertoUSB1.device = "/dev/ttyUSB1"

            Entonces:

            enumerate(puertos, start=1)

            genera en cada iteración:

            Primera iteración:
                indice = 1
                puerto = objetoPuertoUSB0

            Segunda iteración:
                indice = 2
                puerto = objetoPuertoUSB1
            """
            descripcion = puerto.description or "Sin descripción"  # Usa puerto.description si tiene contenido. Si está vacío, usa "Sin descripción". Ejemplo: puerto.description = "CP2102 USB to UART Bridge Controller"
            ni = identificarNiEnPuerto(puerto.device, baudios)     # Intenta descubrir el NI del XBee conectado a ese puerto.
            puertosIdentificados.append((puerto, ni))              # Hace una dupla de los objetos puerto y ni, y la agrega a la lista puertosIdentificados.

            """
            Ejemplo: 

            puertosIdentificados = [
                (objetoPuertoUSB0, "COORDINADOR"),
                (objetoPuertoUSB1, "ROUTER")
            ]
            """

            lineaPuerto = f"  {indice}. {puerto.device} - {descripcion}"    # 1. /dev/ttyUSB0 - CP2102 USB to UART

            if ni:
                lineaPuerto += f" | NI: {ni}"                               # 1. /dev/ttyUSB0 - CP2102 USB to UART | NI: COORDINADOR
            else:
                lineaPuerto += " | NI no disponible"                        # 1. /dev/ttyUSB0 - CP2102 USB to UART | NI no disponible

            print(lineaPuerto)

        print(f"  {len(puertos) + 1}. Escribir otra ruta")

        while True:
            respuesta = input("Seleccione el puerto: ").strip()

            try:
                opcion = int(respuesta)
            except ValueError:
                print("Debe escribir el número de una opción.")
                continue

            if 1 <= opcion <= len(puertos):                                 # Usuario escoge desde la opción 1 hasta la opción n, donde n = len(puertos)
                puerto, ni = puertosIdentificados[opcion - 1]               # Se resta 1 porque la lista puertosIdentificados empieza en índice 0, mientras que las opciones mostradas al usuario empiezan en 1.
                return puerto.device, ni                                    # /dev/ttyUSB0, "COORDINADOR"  o  /dev/ttyUSB1, "ROUTER"

            if opcion == len(puertos) + 1:                                  # Se escogió la opción de escribir otra ruta
                break

            print("Opción no válida.")

    else:                                                                   # si la lista de puertos SÍ está vacía
        print("  No se detectaron puertos automáticamente.")

    ruta = input("Ruta del puerto [/dev/ttyUSB0]: ").strip()                # Si se eligió Escribir otra ruta o no se detectó ningún puerto automáticamente, se le pide al usuario que escriba la ruta del puerto. 
    ruta = ruta or "/dev/ttyUSB0"                                           # Si el usuario no escribe nada, se usa "/dev/ttyUSB0" como valor predeterminado.       
    ni = identificarNiEnPuerto(ruta, baudios)
    return ruta, ni


def abrirPuerto(rutaPuerto, baudios):
    try:                                    # Try: "Intenta hacer lo siguiente, pero prepárate por si ocurre un error"
        return serial.Serial(               # Instrucción que abre el puerto serial
                      
            port=rutaPuerto,                # rutaPuerto = "/dev/ttyUSB0"

            baudrate=baudios,               # baudios = 9600 
            bytesize=serial.EIGHTBITS,      # 8 bits de datos 
            parity=serial.PARITY_NONE,      # Sin bit de paridad  
            stopbits=serial.STOPBITS_ONE,   # 1 bit de parada ----> se está configurando 9600 8N1

            timeout=TIEMPO_ESPERA_SERIAL_S, # cuánto tiempo esperará una operación de lectura antes de rendirse
            write_timeout=2.0,              # cuánto tiempo esperará una operación de escritura antes de rendirse
        )

    except serial.SerialException as error:
        raise ErrorXBee(
            f"no fue posible abrir {rutaPuerto}: {error}"
        ) from error


# *********************** MODO COMANDO AT ************************ #

def leerRespuestaTexto(puerto, tiempoEspera=TIEMPO_RESPUESTA_AT_S): # Si no se especifica el segundo parámetro de tiempo, se usará el valor predeterminado de TIEMPO_RESPUESTA_AT_S = 2.0 segundos.
    tiempoFinal = time.monotonic() + tiempoEspera   # time.monotonic() = tiempo inicial en el que inicia la función. En base a esto se calcula el tiempo final que se necesita para salir del while. tiempoFinal es un valor fijo. 
    respuesta = bytearray()                         # Contenedor vacío para almacenar bytes
    recibioRespuesta = False

    while time.monotonic() < tiempoFinal: # time.monotonic() inicia como el tiempo inicial, pero dentro del while se va actualizando hasta superar el tiempo final. Es decir, cuando pase el tiempo de espera. 
        dato = puerto.read(1)             # Intenta leer un byte del puerto serial  

        if not dato:
            continue

        if dato in (b"\r", b"\n"):                  # Pregunta si el byte recibido es \r o \n
            if dato == b"\r" or recibioRespuesta:   # Si el dato es \r, entonces retorna la respuesta. Si el dato es \n y ya se recibió una respuesta anteriormente (recibioRespuesta == True), también retorna la respuesta. 
                return respuesta.decode(
                    "ascii",
                    errors="replace",               # Si aparece algún byte que no pueda interpretarse como ASCII, no se provoca una excepción y se reemplaza por un carácter especial.
                ).strip()                           # elimina espacios y saltos de línea sobrantes al principio y al final.

            continue                                # Sirve para ignorar un salto de línea inicial

        respuesta.extend(dato)                      # Añade la lista de elementos (byte) al final de la lista actual
        recibioRespuesta = True

    return None


def entrarModoComando(puerto):
    puerto.reset_input_buffer()     # borra todos los bytes que estuvieran pendientes de lectura.
    puerto.reset_output_buffer()    # borra datos pendientes de transmisión que todavía estuvieran en el buffer de salida

    time.sleep(TIEMPO_GUARDA_S) # Por defecto, el Xbee exige un tiempo de silencio de por lo menos 1 segundo antes y después de enviar +++
    puerto.write(b"+++")
    puerto.flush()              # Hace que Python espere hasta que los datos pendientes de escritura hayan sido enviados al sistema serial
    time.sleep(TIEMPO_GUARDA_S)

    respuesta = leerRespuestaTexto(puerto, 0.6) # Se espera un máximo de 0.6 segundos para recibir la respuesta del XBee. Si no llega nada, se retorna None. Si llega algo, se retorna la respuesta decodificada y sin espacios ni saltos de línea al principio y al final.

    if respuesta != "OK":                       # Según el manual, el equipo debe responder con OK\r una vez entre en el modo comnando
        raise ErrorXBee(
            "no fue posible entrar en modo comando. Compruebe TX/RX, GND, "
            "la alimentación de 3.3 V, el puerto y los baudios seleccionados. "
            "La secuencia +++ y los tiempos de guarda deben coincidir con CC/GT."
        )


def enviarComandoTexto(puerto, comando, parametro=None):   # Esta función envía comandos AT al XBee una vez que está en modo comando y recibe su respuesta
    texto = f"AT{comando}"                                 # Ejemplo: enviarComandoTexto(puerto, "NI") ---> ATNI\r

    if parametro is not None:

        parametroTexto = parametro if comando == "NI" else f"{parametro:X}"

        """
        if comando == "NI":
            parametroTexto = parametro
        else:
            parametroTexto = f"{parametro:X}" Si no es NI, se convierte el valor a hexadecimal en mayúsculas como se exige para los parámetros en el manual. Ejemplo: 9 ---> "9", 10 ---> "A", 15 ---> "F", 16 ---> "10"
        """

        texto += parametroTexto


    """
    Si se proporcionó un parámetro, lo añade al comando AT. Esto permite modificar el valor del registro.
    Ejemplo:

    texto = "ATNI"
    parametro = "COORDINADOR"
    resultado: ATNICOORDINADOR
    """

    puerto.reset_input_buffer()                # borra todos los bytes que estuvieran pendientes de lectura.
    puerto.write((texto + "\r").encode("ascii"))  # Se codifica, por ejemplo: ATNICOORDINADOR<CR> a bytes ascii
    puerto.flush()                             # Hace que Python espere hasta que los datos pendientes de escritura hayan sido enviados al sistema serial

    respuesta = leerRespuestaTexto(puerto)    # retorna el arreglo de bytes que se encuentre en el buffer de entrada del puerto serial del XBee.
                                               # Se decodifica de bytes y sin espacios ni saltos de línea al principio y al final. Si no hay respuesta, retorna None.

    if respuesta == "ERROR" or respuesta is None:
        if respuesta == "ERROR":
            raise ErrorXBee(f"el XBee rechazó el comando {texto}")

        raise ErrorXBee(f"el XBee no respondió al comando {texto}")


    # Los comandos que modifican un registro (Por ejemplo, comandos en los que se les manda un parámetro para establecer su valor)
    # tienen que responder "OK" cuando se ejecutan correctamente.
    if parametro is not None:
        if respuesta != "OK":
            raise ErrorXBee(
                f"respuesta inesperada al configurar AT{comando}: {respuesta}"
            )

        return respuesta


    # Los comandos de control, como ATWR y ATCN, responden "OK" cuando se ejecutan correctamente.
    if comando in ("WR", "CN"):
        if respuesta != "OK":
            raise ErrorXBee(
                f"AT{comando} respondió: {respuesta}"
            )

        return respuesta


    # ATNI es la única consulta textual utilizada actualmente por el programa.
    # Puede devolver un nombre o una cadena vacía si todavía no se asignó un NI.
    if comando == "NI":
        return respuesta


    # Las demás consultas utilizadas por el programa devuelven números escritos como texto hexadecimal.
    # Ejemplo: ATID responde "0009" y se devuelve 9.

    if not respuesta:
        raise ErrorXBee(f"AT{comando} devolvió un valor numérico vacío")

    try:
        return int(respuesta, 16)  # Se convierte el texto de la respuesta hexadecimal del comando del XBee a un entero.

    except ValueError as error:
        raise ErrorXBee(
            f"AT{comando} no devolvió un hexadecimal válido: {respuesta!r}"
        ) from error


def salirModoComando(puerto):
    try:
        enviarComandoTexto(puerto, "CN") # ATCN\r ---> CN = Command Mode Exit. Se sale del modo comando y aplica a los cambios pendientes.
    except ErrorXBee:
        # Puede ocurrir si el módulo ya salió por tiempo de espera.
        pass


# *********************** CONSULTAS AT LOCALES ******************** #

def identificarNiEnPuerto(rutaPuerto, baudios):
    """Lee ATNI sin modificar la configuración del XBee conectado."""

    try:
        with abrirPuerto(rutaPuerto, baudios) as puerto:
            entrarModoComando(puerto)

            try:
                ni = enviarComandoTexto(puerto, "NI").strip()
            finally:
                salirModoComando(puerto)

            return ni or None

    except (ErrorXBee, serial.SerialException, OSError, ValueError):
        # El puerto puede pertenecer a otro dispositivo o estar siendo usado.
        return None

    
def leerRegistrosConfiguracion(puerto):
    """Lee los registros locales dentro de una sesión de modo comando ya abierta."""
    configuracion = {}  # Crea un diccionario vacío

    # Un fallo en un registro obligatorio impide identificar/configurar el nodo.
    for comando in COMANDOS_LECTURA_OBLIGATORIOS:                            # COMANDOS_LECTURA_OBLIGATORIOS = ("ID", "AP", "CE", "NI", "SH", "SL")
        configuracion[comando] = enviarComandoTexto(puerto, comando)         # Ejemplo: configuracion["ID"] = enviarComandoTexto(puerto, "ID")

    
    for comando in COMANDOS_LECTURA_ADICIONALES:                             # COMANDOS_LECTURA_ADICIONALES = ("VR", "BD", "HP", "CM", "DH", "DL")
        try:
            configuracion[comando] = enviarComandoTexto(puerto, comando)
        except ErrorXBee:
            configuracion[comando] = None                                    # Algunos firmwares no ofrecen todos los registros adicionales. None significa 'no disponible'


    # SH y SL son dos mitades de la dirección. SH = Serial Number High, SL = Serial Number Low. 
    # Se combinan para formar la dirección MAC completa de 64 bits. Ejemplo: SH=0x0013A200, SL=0x40B9B5D2 ---> MAC=0013A20040B9B5D2
    configuracion["MAC"] = (
        f"{configuracion['SH']:08X}{configuracion['SL']:08X}"
    )

    """
    08X da ocho cifras hexadecimales, las dos mitades forman 16 cifras que es la dirección MAC completa. 
    0  → rellenar con ceros a la izquierda
    8  → ocupar exactamente 8 posiciones como mínimo
    X  → representar el número en hexadecimal, usando A-F mayúsculas
    """

    return configuracion


def leerConfiguracionLocal(puerto):
    """Entra con +++, lee todos los registros y sale con ATCN, incluso si falla."""
    entrarModoComando(puerto)

    try:
        return leerRegistrosConfiguracion(puerto)
    finally:
        # finally también se ejecuta si una consulta genera ErrorXBee.
        salirModoComando(puerto)


def mostrarConfiguracion(configuracion):
    """Muestra los valores leídos; no envía comandos ni modifica el XBee."""
    ap = configuracion["AP"]
    ce = configuracion["CE"]

    print()
    print("Configuración actual del XBee")
    print("-" * 46)
    print(f"MAC SH:SL : {configuracion['MAC']}")
    print(f"NI        : {configuracion['NI'] or '(vacío)'}")
    print(f"ID        : 0x{configuracion['ID']:04X}")                       #04X es formato hexadecimal de 4 dígitos, rellenando con ceros a la izquierda si es necesario. Ejemplo: 9 ---> 0009
    print(f"AP        : {ap} - {NOMBRES_AP.get(ap, 'valor desconocido')}")  # Busca ap dentro del diccionario NOMBRES_AP (recordar que ahora ap = configuracion["AP"] y es un valor) 
    print(f"CE        : {ce} - {NOMBRES_CE.get(ce, 'valor desconocido')}")  # Si no existe, devuelve "valor desconocido"
    # AP describe la operación normal; Lectura describe cómo se consultó.
    # Así AP=1/2 y modo comando AT pueden aparecer juntos sin contradicción.
    print(f"Lectura   : {descripcionModo(MODO_COMANDO)}")                   # Devuelve una descripción textual del modo en que se leyó la configuración. En este caso, siempre, en modo comando

    if configuracion.get("BD") is not None:
        bd = configuracion["BD"]
        velocidad = BAUDIOS_POR_BD.get(bd)  # Ejemplo: BD=3 corresponde a 9600, según la tabla provista por el manual del fabricante.

        if velocidad:
            print(f"BD        : {bd:X} - {velocidad} baudios")
        else:
            print(f"BD        : 0x{bd:X}")  # Si no encontró correspondencia, al menos muestra el valor hexadecimal que leyó

    for comando in ("VR", "HP", "CM"):
        valor = configuracion.get(comando)

        if valor is not None:
            print(f"{comando:<10}: 0x{valor:X}")    # :<10 imprime el texto alineado a la izquirda ocupando 10 caracteres

    if configuracion.get("DH") is not None and configuracion.get("DL") is not None:
        print(
            "DH:DL     : "
            f"{configuracion['DH']:08X}{configuracion['DL']:08X}"
        )

    print("-" * 46)


# ******************** ENTRADA DE PARÁMETROS ********************* #

def pedirId(valorActual):
    """Solicita el ID hexadecimal; Enter conserva el valor actual."""
    while True:
        respuesta = input(
            f"ID de red en hexadecimal [0x{valorActual:04X}]: "
        ).strip()

        if not respuesta:
            return valorActual
        
        """
        if respuesta.lower().startswith("0x"):
            respuesta = respuesta[2:]  # Acepta tanto '0x0009' como '0009'. Si el usuario pone '0x0009' se pasa a '0009'
        """

        try:
            valor = int(respuesta, 16)                      # lo convierte en entero
        except ValueError:
            print("El ID debe ser un número hexadecimal.")
            continue

        if 0 <= valor <= 0x7FFF:  # Rango del firmware 900HP de este proyecto según el manual es de máximo 0x7FFF = 32767 en entero.
            return valor

        print("Para el XBee-PRO 900HP, ID debe estar entre 0x0000 y 0x7FFF.")  # Si no se ejecuta el return, no se sale de la funcion y se imprime el mensaje de error y vuelve a pedir el ID.


def pedirAp(valorActual):
    """ Pide el modo de operación serial que tendrá el XBee después de salir de modo comando.
        Solo cambia el formato usado durante la operación normal del módulo.
    """
    print()
    print("AP - Modo de operación serial")
    print("  0. Transparent Mode")
    print("  1. API Mode Without Escapes")
    print("  2. API Mode With Escapes")
    print("Recomendación: Coordinador = 1; Router = 0.")

    while True:
        respuesta = input(f"Seleccione AP [{valorActual}]: ").strip()

        if not respuesta:
            return valorActual

        if respuesta in ("0", "1", "2"):
            return int(respuesta)

        print("AP solamente puede ser 0, 1 o 2.")


def pedirCe(valorActual):
    """Conserva las opciones de rol utilizadas en el montaje: CE=0 o CE=1."""
    print()
    print("CE - Routing/Messaging Mode")
    print("  0. Standard Router")
    print("  1. Indirect Msg Coordinator")
    print("Recomendación: Coordinador = 1; Router = 0.")

    # Si el módulo tenía otro CE, se propone 0 dentro de las opciones del menú.
    valorPredeterminado = valorActual if valorActual in (0, 1) else 0

    while True:
        respuesta = input(f"Seleccione CE [{valorPredeterminado}]: ").strip()

        if not respuesta:
            return valorPredeterminado

        if respuesta in ("0", "1"):
            return int(respuesta)

        print("Con este configurador, CE solamente puede ser 0 o 1.")


def pedirNi(valorActual):
    """Pide un nombre ASCII de hasta 20 caracteres y evita separadores de comandos."""
    while True:
        respuesta = input(f"NI - Nombre del nodo [{valorActual}]: ").strip() # .strip() elimina espacios, tabuladores y saltos de línea al principio y al final.
                                                                             # "   ROUTER   ".strip() ---> "ROUTER"
        if not respuesta:
            respuesta = valorActual

        try:
            datos = respuesta.encode("ascii")                       # Aquí se intenta convertir el texto Python a bytes ASCII.
        except UnicodeEncodeError:
            print("NI solamente puede contener caracteres ASCII.")
            continue

        if not datos:
            print("NI no puede quedar vacío.")
            continue

        if len(datos) > 20:                                           # cada caracter ASCII ocupa un byte, por lo que len(datos) devuelve la cantidad de bytes que ocupa el nombre. Si es mayor a 20, se rechaza.
            print("NI puede tener como máximo 20 caracteres ASCII.")
            continue

        if "," in respuesta or "\r" in respuesta or "\n" in respuesta: # \r es Carriage Return y \n es Line Feed. Ambos tienen significado especial en protocolos de texto y terminación de comandos.
            print("NI no puede contener comas ni saltos de línea.")    # La coma se evita porque en modo comando AT puede usarse como separador entre comandos.
            continue

        # ASCII también contiene caracteres de control, como tabulador y ESC.
        # Para un nombre solo se admiten caracteres imprimibles.
        if any(byte < 0x20 or byte > 0x7E for byte in datos):                   # Los caracteres imprimibles normales están entre 0x20 y 0x7E
            print("NI solamente puede contener caracteres ASCII imprimibles.")
            continue

        return respuesta    # Si se llega a este punto, significa que el nombre ingresado es válido y se retorna.


def pedirNuevaConfiguracion(configuracionActual):
    """Reúne y muestra los cuatro valores; todavía no escribe nada en el XBee."""
    print()
    print("Introduzca la nueva configuración.")
    print("Presione Enter para conservar el valor mostrado entre corchetes.")
    print()

    nueva = {
        "ID": pedirId(configuracionActual["ID"]),
        "AP": pedirAp(configuracionActual["AP"]),
        "CE": pedirCe(configuracionActual["CE"]),
        "NI": pedirNi(configuracionActual["NI"]),
    }

    print()
    print("Configuración que se escribirá")
    print(f"  ID: 0x{nueva['ID']:04X}")
    print(f"  AP: {nueva['AP']} - {NOMBRES_AP[nueva['AP']]}")
    print(f"  CE: {nueva['CE']} - {NOMBRES_CE[nueva['CE']]}")
    print(f"  NI: {nueva['NI']}")

    return nueva


# ********************* ESCRITURA Y VERIFICACIÓN ***************** #

def configurarEnModoComando(puerto, nuevaConfiguracion):
    """Entra con +++, escribe los cuatro parámetros, guarda y sale con ATCN."""

    entrarModoComando(puerto)

    try:
        for comando in ("ID", "CE", "NI", "AP"):

            parametro = nuevaConfiguracion[comando]

            # Mediante parametro se está estableciendo un nuevo valor al registro del XBee. Ejemplo:enviarComandoTexto(puerto, "ID", "1A") ---> ATID1A\r
            # Cada vez que se le cambia un valor a un registro mediante un comando, el programa responde con OK. Si no responde OK, significa que hubo un problema.
            enviarComandoTexto(puerto, comando, parametro)

        # WR escribe en memoria no volátil. Envía ATWR para conservar la configuración después de apagar el módulo.
        # Los comandos de control como ATWR también tienen que devolver OK.
        enviarComandoTexto(puerto, "WR")

        time.sleep(0.250)  # Se deja una pequeña pausa antes de transmitir el siguiente comando.

    finally:
        # Un error no debe dejar intencionalmente el módulo en modo comando. Esto no revierte escrituras parciales
        salirModoComando(puerto)


def comprobarConfiguracion(configuracionLeida, configuracionEsperada):
    """Devuelve una lista de diferencias; una lista vacía significa coincidencia."""
    diferencias = []

    for comando in ("ID", "AP", "CE", "NI"):
        if configuracionLeida[comando] != configuracionEsperada[comando]:   
            diferencias.append(
                f"{comando}: esperado {configuracionEsperada[comando]!r}, " # !r Es especialmente útil para distinguir texto de números. Para valor = "COORDINADOR", f"{valor}" devuelve COORDINADOR, mientras que f"{valor!r}" devuelve 'COORDINADOR', incluyendo las comillas simples. Esto ayuda a ver si hay espacios o caracteres invisibles en el texto.
                f"leído {configuracionLeida[comando]!r}"                    # añade a la lista algo como "AP: esperado 1, leído 0". Entonces diferencias = ["AP: esperado 1, leído 0"]
            )

    return diferencias


# *************************** REGISTRO JSON**************************** #

def cargarRegistro():
    """Su propósito es obtener el contenido actual del JSON y devolverlo como un diccionario de Python"""

    if not RUTA_REGISTRO.exists():  # pregunta si el archivo existe. Si no existe, devuelve una estructura inicial del registro. Si por ejemplo registro = cargarRegistro(), y el archivo no existe, entonces registro = {"version": 1, "actualizado": None, "modulos": [], "ultimo_descubrimiento": None}
        return {
            "version": 1,
            "actualizado": None,
            "modulos": [],
            "ultimo_descubrimiento": None,
        }  


    try:    # with: cierra automáticamente el archivo, incluso si ocurre un error.
        with RUTA_REGISTRO.open("r", encoding="utf-8") as archivo:  # Si el archivo existe, RUTA_REGISTRO.open(...) abre en modo lectura "r" el archivo cuya ubicación está guardada en RUTA_REGISTRO. encoding="utf-8" indica cómo deben interpretarse los caracteres del archivo. "as archivo" guarda el archivo abierto en una variable llamada archivo
            registro = json.load(archivo)                           # json.load(archivo) lee el JSON y lo convierte en estructuras de Python

            """

            Por ejemplo, si el archivo contiene:

            {
            "version": 1,
            "modulos": []
            }

            registro = json.load(archivo)

            Produce: 

            registro = {
                "version": 1,
                "modulos": [],
            }

            """

    except (OSError, json.JSONDecodeError) as error:
        raise ErrorXBee(
            f"no fue posible leer {RUTA_REGISTRO.name}: {error}"
        ) from error

    # setdefault añade una clave solo si no existía. Conserva registros previos.
    registro.setdefault("version", 1)   
    registro.setdefault("actualizado", None)
    registro.setdefault("modulos", [])                  
    registro.setdefault("ultimo_descubrimiento", None)
    return registro


def escribirRegistro(registro):
    """Recibe un diccionario y lo guarda en xbee_configurados.json"""
    registro["actualizado"] = fechaHoraActual()
    rutaTemporal = RUTA_REGISTRO.with_suffix(".json.tmp")   # Crear una ruta temporal xbee_configurados.json.tmp

    try:
        with rutaTemporal.open("w", encoding="utf-8") as archivo:       # Escribir primero el archivo temporal
            json.dump(registro, archivo, indent=4, ensure_ascii=False)  # Escribe registro a archivo. json.dump() convierte el diccionario de Python en JSON
            archivo.write("\n")                                         # añade un salto de línea al final del archivo.

        rutaTemporal.replace(RUTA_REGISTRO)                             # Solo después de terminar correctamente la escritura se reemplaza el JSON anterior del arhivo termporal al archivo de la ruta verdadera.
    except OSError as error:
        raise ErrorXBee(
            f"no fue posible escribir {RUTA_REGISTRO.name}: {error}"
        ) from error


def guardarModuloEnRegistro(configuracion, rutaPuerto):
    """Esta función añade un XBee nuevo o actualiza uno ya registrado, identificándolo por su MAC."""
    registro = cargarRegistro()
    mac = configuracion["MAC"]

    modulo = {
        "mac": mac,
        "ni": configuracion["NI"],
        "id": f"0x{configuracion['ID']:04X}",
        "ap": configuracion["AP"],
        "ce": configuracion["CE"],
        "funcion": NOMBRES_CE.get(
            configuracion["CE"],
            "Desconocida",
        ),
        "puerto_usado": rutaPuerto,
        "fecha_configuracion": fechaHoraActual(),
    }

    # La MAC no cambia si cambia el NI o Linux cambia el número ttyUSB.
    for indice, anterior in enumerate(registro["modulos"]):
        if anterior.get("mac") == mac:
            registro["modulos"][indice] = modulo
            break

    else:  # El else del for se ejecuta solo si no se encontró una MAC y no hubo break. ---> for ... else Ejecuta el else solamente si el for termina de recorrer todos sus elementos sin haber ejecutado un break.
        registro["modulos"].append(modulo) # La lista registro["modulos"] = [] pasa a [{"mac": "0013A20040B9B5D2", "ni": "COORDINADOR", "id": "0x0009", "ap": 1, "ce": 1, "funcion": "Indirect Msg Coordinator", "puerto_usado": "/dev/ttyUSB0", "fecha_configuracion": "2024-06-15 12:34:56"}]
                                           # De esta manera se crea el primer elemento de la lista registro["modulos"]


    """
    Supongamos que: 

    registro["modulos"] = [
    {
        "mac": "AAA111",
        "ni": "ROUTER",
        "id": "0x0009"
    },
    {
        "mac": "BBB222",
        "ni": "COORDINADOR",
        "id": "0x0009"
    }
    ]
    Y acabas de configurar un módulo cuya MAC es: 
    
    mac = configuracion["MAC"] ="BBB222"

    Y además, modulo contiene la información nueva de ese XBee

    modulo = {
    "mac": "BBB222",
    "ni": "NUEVO_NOMBRE",
    "id": "0x0009",
    ...
    }

    Ahora empieza: 

    for indice, anterior in enumerate(registro["modulos"]):     # Donde enumerate() toma cada elemento de la lista y además te da su posición.

    Primera vuelta:

    indice = 0

    anterior = {
        "mac": "AAA111",
        "ni": "ROUTER",
        "id": "0x0009"
    }

    Y entonces anterior.get("mac") = "AAA111" que no es igual a mac = "BBB222", entonces no entra al if y sigue con la siguiente vuelta del for.

    Segunda vuelta:

    indice = 1

    anterior = {
        "mac": "BBB222",
        "ni": "COORDINADOR",
        "id": "0x0009"
    }

    Ahora anterior.get("mac") = "BBB222" que es igual a mac = "BBB222", entonces entra al if y ejecuta:

    registro["modulos"][indice] = modulo

    como inidice = 1, entonces registro["modulos"][1] = modulo, es decir, se reemplaza el segundo elemento de la lista por el nuevo diccionario modulo. Y luego hace break y sale del for.

    Como hubo break, entonces el else no se ejecuta y no se añade un nuevo elemento a la lista. La lista sigue teniendo dos elementos, pero el segundo ahora tiene la información actualizada del XBee.
    """                     
                     
    escribirRegistro(registro)  # Guarda ese diccionario en: xbee_configurados.json


def mostrarRegistro():
    """Muestra lo guardado en el JSON"""
    registro = cargarRegistro()
    modulos = registro["modulos"]

    print()
    print(f"Registro: {RUTA_REGISTRO}")

    if not modulos:
        print("Todavía no se han registrado módulos.")
        return

    print()

    for indice, modulo in enumerate(modulos, start=1):
        print(
            f"{indice}. {modulo.get('ni', '(sin NI)')} | "
            f"MAC {modulo.get('mac', '?')} | "
            f"ID {modulo.get('id', '?')} | "
            f"AP {modulo.get('ap', '?')} | "
            f"CE {modulo.get('ce', '?')}"
        )


# *********************** TRAMAS API XBEE ************************ #

# Estas funciones solo se utilizan durante el descubrimiento ND.
# No se necesitan para leer ni escribir ID, AP, CE, NI, NO o NT.
# 'modo' es MODO_API_1 o MODO_API_2: son constantes de texto del programa,
# no los números 1 y 2 que se guardan en el parámetro AP del XBee.

def escaparDatosApi(datos):
    """Prepara los bytes reservados para transmitirlos en API 2."""
    resultado = bytearray()

    for byte in datos:  # Al recorrer bytes se obtiene un entero de 0 a 255.
        if byte in (0x7E, 0x7D, 0x11, 0x13):
            resultado.append(0x7D)         # Avisa que el siguiente byte está escapado.
            resultado.append(byte ^ 0x20)  # ^ es XOR; no es una potencia.
        else:
            resultado.append(byte)

    # Ejemplo: 0x7E se transmite como 0x7D 0x5E.
    # El receptor recupera 0x7E aplicando 0x5E ^ 0x20.
    # No se cifra ni se modifica el significado de los datos.
    return bytes(resultado)


def crearTramaApi(datosTrama, modo):
    """Añade inicio, longitud y checksum a los datos internos de una trama."""
    if modo not in (MODO_API_1, MODO_API_2):
        raise ErrorXBee("para crear una trama debe seleccionarse API 1 o API 2")

    # La longitud cuenta solo datosTrama, antes de agregar escapes.
    # to_bytes(2, 'big') ocupa dos bytes: el más significativo va primero.
    # Ejemplo: cuatro bytes de datos ---> longitud b'\x00\x04'.
    longitud = len(datosTrama).to_bytes(2, byteorder="big")

    # & 0xFF conserva los ocho bits inferiores del resultado.
    # La suma de datosTrama + checksum debe terminar en 0xFF.
    # No se incluyen el delimitador ni la longitud en esta suma.
    checksum = bytes([(0xFF - sum(datosTrama)) & 0xFF])
    contenido = longitud + datosTrama + checksum

    if modo == MODO_API_2:
        contenido = escaparDatosApi(contenido)

    return b"\x7E" + contenido  # El delimitador inicial nunca se escapa.


def leerByteApi(puerto, modo, tiempoFinal):
    """Lee un byte lógico; en API 2 puede necesitar dos bytes del puerto."""
    escapado = False

    while time.monotonic() < tiempoFinal:
        dato = puerto.read(1)

        if not dato:
            continue

        byte = dato[0]  # b'\x88'[0] es el entero 136, equivalente a 0x88.

        if escapado:
            return byte ^ 0x20

        if modo == MODO_API_2 and byte == 0x7D:
            # La segunda mitad puede llegar en otra lectura. Se sigue esperando
            # hasta tiempoFinal; una lectura vacía no implica un escape roto.
            escapado = True
            continue

        return byte

    if escapado:
        raise ErrorXBee("se recibió un escape API incompleto")

    raise TimeoutError


def leerTramaApi(puerto, modo, tiempoEspera=TIEMPO_RESPUESTA_API_S):
    """Reconstruye una trama, comprueba su checksum y devuelve sus datos internos.

    Resultado: tipo de trama + campos específicos de ese tipo.
    No devuelve delimitador, longitud ni checksum. Tampoco interpreta ND;
    esa tarea corresponde a descubrirNodosApi y analizarRespuestaNd.
    """
    if modo not in (MODO_API_1, MODO_API_2):
        raise ErrorXBee("para leer una trama debe seleccionarse API 1 o API 2")

    # Todas las lecturas comparten el mismo límite, que no se reinicia por byte.
    # read(1) conserva el timeout corto configurado en abrirPuerto.
    tiempoFinal = time.monotonic() + tiempoEspera

    # Busca el delimitador inicial. Este byte nunca se escapa.
    while time.monotonic() < tiempoFinal:
        dato = puerto.read(1)

        if dato == b"\x7E":
            break
    else:
        raise TimeoutError

    byteLongitudAlto = leerByteApi(puerto, modo, tiempoFinal)
    byteLongitudBajo = leerByteApi(puerto, modo, tiempoFinal)
    # << 8 desplaza el byte alto ocho posiciones; | reúne ambos bytes.
    # Ejemplo: 0x01 0x02 ---> 1 * 256 + 2 = 258 bytes.
    longitud = (byteLongitudAlto << 8) | byteLongitudBajo

    if longitud == 0:
        raise ErrorXBee("se recibió una trama API sin tipo de trama")

    datosTrama = bytearray()

    for _ in range(longitud):  # _ indica que no necesitamos el número de la vuelta.
        datosTrama.append(leerByteApi(puerto, modo, tiempoFinal))

    checksum = leerByteApi(puerto, modo, tiempoFinal)

    if ((sum(datosTrama) + checksum) & 0xFF) != 0xFF:
        raise ErrorXBee("se recibió una trama API con checksum incorrecto")

    return bytes(datosTrama)


def siguienteIdTrama():
    """Genera 1, 2, ..., 255, 1, ... para relacionar ND con sus respuestas."""
    # % obtiene el resto de la división. Si el valor era 255, vuelve a 1.
    # Se evita 0 porque desactiva la respuesta del comando API 0x08.
    siguienteIdTrama.valor = (siguienteIdTrama.valor % 255) + 1
    return siguienteIdTrama.valor


# Las funciones son objetos de Python y pueden tener atributos.
# .valor recuerda el último identificador entre llamadas a siguienteIdTrama().
siguienteIdTrama.valor = 0



# ********************* DESCUBRIMIENTO DE RED ******************** #

def prepararDescubrimientoApi(puerto):
    """Lee por AT los datos necesarios y confirma ATCN antes de permitir API.

    Devuelve (configuracion, modoApi, opcionesNo, tiempoNt).
    NO indica los campos adicionales de ND y NT su duración en décimas
    de segundo. Consultarlos no cambia sus valores ni el AP del XBee.
    """
    entrarModoComando(puerto)
    salidaConfirmada = False

    try:
        configuracion = leerRegistrosConfiguracion(puerto)
        ap = configuracion["AP"]

        if ap not in (1, 2):
            raise ErrorXBee(
                f"el XBee local tiene AP={ap}. Esta opción utiliza ND mediante "
                "tramas 0x08/0x88 y necesita AP=1 o AP=2. Puede elegirlo en la "
                "opción 2; no se cambiará AP automáticamente"
            )

        opcionesNo = enviarComandoTexto(puerto, "NO")
        tiempoNt = enviarComandoTexto(puerto, "NT") * 0.100
        modoApi = MODO_API_1 if ap == 1 else MODO_API_2

        # En esta transición es indispensable saber que ATCN fue aceptado.
        # salirModoComando tolera fallos para tareas de limpieza, por eso aquí
        # se verifica explícitamente OK antes de enviar cualquier trama API.
        respuesta = enviarComandoTexto(puerto, "CN")

        if respuesta != "OK":
            raise ErrorXBee(f"no se confirmó la salida de modo comando: {respuesta}")

        salidaConfirmada = True
    finally:
        if not salidaConfirmada:
            salirModoComando(puerto)

    time.sleep(0.100)  # Pequeña separación entre la sesión de texto y ND binario.
    return configuracion, modoApi, opcionesNo, tiempoNt


def analizarRespuestaNd(datos, opcionesNo):
    """Extrae un nodo del contenido ND del firmware 900HP, no de una trama completa.

    Los primeros nueve bytes son SH (4), SL (4) y RSSI (1).
    Después viene NI, terminado en 0x00, y seis bytes de campos básicos.
    Los campos adicionales dependen de los bits activos en NO.
    Este formato es específico de 900HP; no es el formato ND de Zigbee.
    """
    if len(datos) < 16:
        raise ErrorXBee("se recibió una respuesta ND demasiado corta")

    # En un corte [inicio:fin], fin no se incluye.
    sh = bytesAEntero(datos[0:4])
    sl = bytesAEntero(datos[4:8])
    rssi = -datos[8]  # Ejemplo: el byte 64 representa -64 dBm.

    try:
        finNi = datos.index(0x00, 9)  # Busca el terminador a partir del byte 9.
    except ValueError as error:
        raise ErrorXBee("la respuesta ND no contiene el final de NI") from error

    if finNi - 9 > 20:
        raise ErrorXBee("la respuesta ND contiene un NI mayor de 20 caracteres")

    ni = datos[9:finNi].decode("ascii", errors="replace")
    posicion = finNi + 1

    if len(datos) < posicion + 6:
        raise ErrorXBee("la respuesta ND no contiene todos los campos básicos")

    tipoNodo = datos[posicion]
    estado = datos[posicion + 1]
    profileId = int.from_bytes(datos[posicion + 2:posicion + 4], "big")
    manufacturerId = int.from_bytes(datos[posicion + 4:posicion + 6], "big")
    posicion += 6

    digiDeviceType = None
    rssiUltimoSalto = None

    # & comprueba un bit sin exigir que los demás sean cero.
    # NO=5 (binario 101) activa tanto 0x01 como 0x04.
    if opcionesNo & 0x01:
        if len(datos) < posicion + 4:
            raise ErrorXBee("la respuesta ND no contiene el tipo Digi indicado por NO")

        digiDeviceType = int.from_bytes(datos[posicion:posicion + 4], "big")
        posicion += 4

    if opcionesNo & 0x04:
        if len(datos) <= posicion:
            raise ErrorXBee("la respuesta ND no contiene el RSSI adicional indicado por NO")

        rssiUltimoSalto = -datos[posicion]

    # Se devuelven valores ya interpretados para mostrarlos o guardarlos en JSON.
    return {
        "mac": f"{sh:08X}{sl:08X}",
        "sh": f"{sh:08X}",
        "sl": f"{sl:08X}",
        "ni": ni,
        "rssi_dbm": rssi,
        "tipo_nodo": tipoNodo,
        "tipo_nodo_texto": NOMBRES_TIPO_NODO.get(
            tipoNodo,
            f"Desconocido ({tipoNodo})",
        ),
        "estado": estado,
        "profile_id": f"0x{profileId:04X}",
        "manufacturer_id": f"0x{manufacturerId:04X}",
        "digi_device_type": (
            f"0x{digiDeviceType:08X}"
            if digiDeviceType is not None
            else None
        ),
        "rssi_ultimo_salto_dbm": rssiUltimoSalto,
    }


def descubrirNodosApi(puerto, modo, opcionesNo, tiempoNt):
    """Envía únicamente ND por API y reúne las respuestas de los distintos nodos.

    Debe llamarse después de prepararDescubrimientoApi. NO y NT ya se leyeron
    por AT; esta función no consulta ni escribe otros registros del XBee.
    El XBee local debe estar fuera de modo comando, operando en API 1 o API 2.
    """
    # Se conceden dos segundos adicionales, sin exceder el límite del programa.
    tiempoEspera = min(
        tiempoNt + 2.0,
        TIEMPO_MAXIMO_DESCUBRIMIENTO_S,
    )

    print()
    print(f"Ejecutando ND. Tiempo máximo de espera: {tiempoEspera:.1f} s...")

    if tiempoNt + 2.0 > TIEMPO_MAXIMO_DESCUBRIMIENTO_S:
        print(
            "Aviso: NT solicita una espera mayor; esta versión la limita a "
            f"{TIEMPO_MAXIMO_DESCUBRIMIENTO_S:.0f} s."
        )

    identificador = siguienteIdTrama()
    # 0x08 = comando AT local. El XBee local ejecuta ND para buscar por radio.
    # No es una configuración remota 0x17 y no incluye las letras 'AT' ni CR.
    # Con identificador=1, crearTramaApi produce: 7E 00 04 08 01 4E 44 64.
    datosTrama = bytes([0x08, identificador]) + b"ND"

    puerto.reset_input_buffer()
    puerto.write(crearTramaApi(datosTrama, modo))
    puerto.flush()

    tiempoFinal = time.monotonic() + tiempoEspera
    nodosPorMac = {}  # Una MAC es una clave única: evita duplicar un mismo nodo.

    while time.monotonic() < tiempoFinal:
        restante = tiempoFinal - time.monotonic()

        try:
            respuesta = leerTramaApi(puerto, modo, restante)
        except TimeoutError:
            break

        # Formato de respuesta: [0x88, ID, 'N', 'D', estado, ...datos ND...].
        # Se ignoran otros tipos, como 0x90 (datos), que no corresponden a ND.
        if len(respuesta) < 5 or respuesta[0] != 0x88:
            continue

        # Un 0x88 por sí solo no basta: debe coincidir el ID y también el comando.
        if respuesta[1] != identificador or respuesta[2:4] != b"ND":
            continue

        estado = respuesta[4]  # 0 significa que el comando se ejecutó sin error.

        if estado != 0:
            descripcion = NOMBRES_ESTADO_AT.get(estado, str(estado))
            raise ErrorXBee(f"la búsqueda ND terminó con estado {descripcion}")

        datos = respuesta[5:]  # Solo el contenido específico de un resultado ND.

        # Una respuesta sin datos marca el final del descubrimiento.
        if not datos:
            break

        try:
            nodo = analizarRespuestaNd(datos, opcionesNo)
        except ErrorXBee as error:
            print(f"Aviso: se ignoró una respuesta ND: {error}")
            continue

        # A diferencia de una consulta AT sencilla, no se retorna aquí:
        # se sigue leyendo porque pueden responder más módulos.
        nodosPorMac[nodo["mac"]] = nodo
        print(
            f"  Encontrado: {nodo['ni'] or '(sin NI)'} | "
            f"{nodo['mac']} | {nodo['rssi_dbm']} dBm"
        )

    return list(nodosPorMac.values())


def mostrarNodosDescubiertos(nodos):
    """Presenta la lista obtenida por ND; no realiza una nueva búsqueda."""
    print()

    if not nodos:
        print("No se encontraron módulos remotos en la misma red.")
        return

    print(f"Módulos encontrados: {len(nodos)}")
    print("-" * 74)

    for indice, nodo in enumerate(nodos, start=1):
        print(f"{indice}. NI: {nodo['ni'] or '(sin NI)'}")
        print(f"   MAC: {nodo['mac']}")
        print(f"   Tipo: {nodo['tipo_nodo_texto']}")
        print(f"   RSSI informado por ND: {nodo['rssi_dbm']} dBm")

        if nodo["rssi_ultimo_salto_dbm"] is not None:
            print(
                "   RSSI del último salto: "
                f"{nodo['rssi_ultimo_salto_dbm']} dBm"
            )

    print("-" * 74)


def guardarDescubrimiento(configuracionLocal, nodos):
    """Guarda la última búsqueda sin borrar el historial de módulos configurados."""
    registro = cargarRegistro()
    registro["ultimo_descubrimiento"] = {
        "fecha": fechaHoraActual(),
        "id_red": f"0x{configuracionLocal['ID']:04X}",
        "coordinador_local": configuracionLocal["MAC"],  # Clave conservada del JSON anterior.
        "nodos": nodos,
    }
    escribirRegistro(registro)


# ********************** OPERACIONES DEL MENÚ ******************** #

def operacionLeer(rutaPuerto, baudios):
    """Opción 1: abre el puerto, consulta todo por AT y actualiza el NI del menú."""
    with abrirPuerto(rutaPuerto, baudios) as puerto:
        configuracion = leerConfiguracionLocal(puerto)

    mostrarConfiguracion(configuracion)
    return configuracion["NI"] or None


def operacionConfigurar(rutaPuerto, baudios):
    """Opción 2: lee, pide confirmación, escribe y verifica usando solo texto AT."""
   
    with abrirPuerto(rutaPuerto, baudios) as puerto:
        configuracionActual = leerConfiguracionLocal(puerto)

    mostrarConfiguracion(configuracionActual)
    
    nuevaConfiguracion = pedirNuevaConfiguracion(configuracionActual)

    if not pedirConfirmacion("¿Desea escribir estos valores en el XBee?"):
        print("Configuración cancelada. No se modificó el XBee.")
        return configuracionActual["NI"] or None

    print()
    print("Escribiendo configuración...")

    with abrirPuerto(rutaPuerto, baudios) as puerto:
        configurarEnModoComando(puerto, nuevaConfiguracion)

    # Se abre nuevamente el puerto para comprobar el modo activo y los valores.
    time.sleep(0.500)

    with abrirPuerto(rutaPuerto, baudios) as puerto:
        configuracionFinal = leerConfiguracionLocal(puerto)

    diferencias = comprobarConfiguracion(
        configuracionFinal,
        nuevaConfiguracion,
    )
    mostrarConfiguracion(configuracionFinal)

    if diferencias:
        print("La verificación encontró diferencias:")

        for diferencia in diferencias:
            print(f"  - {diferencia}")  # diferencias = ["AP: esperado 1, leído 0"]

        """
        La verificación encontró diferencias:
        - AP: esperado 1, leído 0
        """
        raise ErrorXBee("la configuración no quedó verificada")

    # Solo se registra como configurado si la lectura posterior coincide.
    # Esta lectura verifica registros; no sustituye una prueba de apagado real.
    guardarModuloEnRegistro(configuracionFinal, rutaPuerto)

    print("Configuración guardada y verificada correctamente.")
    print(f"Registro actualizado: {RUTA_REGISTRO.name}")
    return configuracionFinal["NI"] or None


def operacionDescubrir(rutaPuerto, baudios):
    """Opción 3: preparación AT local, salida confirmada y descubrimiento API."""
    with abrirPuerto(rutaPuerto, baudios) as puerto:
        configuracion, modo, opcionesNo, tiempoNt = prepararDescubrimientoApi(puerto)
        mostrarConfiguracion(configuracion)
        print(f"Descubrimiento: {descripcionModo(modo)}; únicamente ND.")

        print(
            "ND solamente encontrará módulos que ya compartan ID, HP, CM "
            "y una configuración de radio compatible."
        )

        nodos = descubrirNodosApi(puerto, modo, opcionesNo, tiempoNt)

    mostrarNodosDescubiertos(nodos)
    guardarDescubrimiento(configuracion, nodos)
    print(f"Resultado guardado en {RUTA_REGISTRO.name}.")
    return configuracion["NI"] or None


def mostrarMenu(rutaPuerto, baudios, niPuertoActual=None):
    """Dibuja el menú y el NI conocido del puerto, sin consultar nuevamente el radio."""
    print()
    print("=" * 58)
    print("CONFIGURADOR DE RED XBEE-PRO 900HP DIGIMESH")
    print("=" * 58)

    lineaPuerto = f"Puerto actual: {rutaPuerto} | {baudios} baudios"

    if niPuertoActual:
        lineaPuerto += f" | NI: {niPuertoActual}"

    print(lineaPuerto)
    print()
    print("  1. Leer configuración del XBee local")
    print("  2. Configurar ID, AP, CE y NI")
    print("  3. Descubrir módulos de la misma red con ND")
    print("  4. Mostrar registro de módulos")
    print("  5. Cambiar puerto serial")
    print("  0. Salir")


# ************************ PROGRAMA PRINCIPAL ********************* #

def main():
    """Procesa los argumentos y mantiene el menú activo hasta elegir Salir."""
    # argparse permite, por ejemplo:
    # python3 configurador_red_xbee_v3.py --puerto /dev/ttyUSB0 --baudios 9600
    analizador = argparse.ArgumentParser(
        description=(
            "Configura XBee-PRO 900HP DigiMesh por CP2102 sin utilizar XCTU."
        )
    )
    analizador.add_argument(
        "--puerto",
        help="Puerto serial, por ejemplo /dev/ttyUSB0",
    )
    analizador.add_argument(
        "--baudios",
        type=int,
        default=BAUDIOS,
        help=f"Velocidad serial actual del XBee; predeterminado {BAUDIOS}",
    )
    argumentos = analizador.parse_args()

    baudios = argumentos.baudios
    rutaPuerto, niPuertoActual = seleccionarPuerto(
        argumentos.puerto,
        baudios,
    )

    # Las opciones 1, 2 y 3 devuelven NI para actualizar el encabezado.
    # No hay ningún proceso de sensores ni lector serial trabajando en paralelo.
    while True:
        mostrarMenu(rutaPuerto, baudios, niPuertoActual)
        opcion = input("Seleccione una opción: ").strip()

        try:
            if opcion == "1":
                niPuertoActual = operacionLeer(rutaPuerto, baudios)

            elif opcion == "2":
                niPuertoActual = operacionConfigurar(rutaPuerto, baudios)

            elif opcion == "3":
                niPuertoActual = operacionDescubrir(rutaPuerto, baudios)

            elif opcion == "4":
                mostrarRegistro()

            elif opcion == "5":
                rutaPuerto, niPuertoActual = seleccionarPuerto(
                    baudios=baudios,
                )

            elif opcion == "0":
                print("Programa finalizado.")
                break

            else:
                print("Opción no válida.")

        except KeyboardInterrupt:
            print("\nOperación cancelada por el usuario.")

        except (ErrorXBee, serial.SerialException, OSError, ValueError) as error:
            # Se informa el fallo y se vuelve al menú; no se anuncia éxito.
            print(f"\nError: {error}")


if __name__ == "__main__":
    # Al importar el archivo para pruebas no se abre ningún puerto ni menú.
    main()
