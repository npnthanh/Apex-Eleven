"""Fictional player identities, with name components grouped by nationality.

Components are in display order: family name first for VN, JP and KR.
Generate an identity once and persist the name and nationality together.
"""
import random


NAME_POOLS = {
    "VN": (
        ("Nguyễn", "Trần", "Lê", "Phạm", "Hoàng", "Vũ", "Đặng", "Bùi", "Đỗ", "Hồ", "Phan", "Võ", "Dương", "Trương", "Đinh", "Lương"),
        ("Minh Huy", "Quốc Nam", "Tuấn Khang", "Hoàng Đức", "Hữu Phúc", "Bảo An", "Thanh Tùng", "Đăng Khôi", "Thái Sơn", "Hải Long", "Duy Anh", "Quang Vinh", "Việt Thắng", "Đức Minh", "Gia Hưng", "Trung Kiên"),
    ),
    "EN": (
        ("Oliver", "Harry", "George", "Jack", "Charlie", "Thomas", "William", "James", "Henry", "Alfie", "Freddie", "Archie", "Ethan", "Samuel", "Daniel", "Joshua", "Nathan", "Lewis", "Callum", "Owen"),
        ("Smith", "Johnson", "Brown", "Taylor", "Wilson", "Davies", "Evans", "Walker", "Wright", "Robinson", "Thompson", "Hughes", "Edwards", "Green", "Bennett", "Foster", "Turner", "Parker", "Collins", "Mitchell"),
    ),
    "FR": (
        ("Louis", "Gabriel", "Raphaël", "Arthur", "Jules", "Hugo", "Lucas", "Nathan", "Léo", "Antoine", "Clément", "Étienne", "Mathis", "Maxime", "Alexandre", "Baptiste", "Adrien", "Théo", "Lucien", "Rémi"),
        ("Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit", "Durand", "Leroy", "Moreau", "Simon", "Laurent", "Lefebvre", "Michel", "Garcia", "Roux", "Fournier", "Girard", "André", "Mercier"),
    ),
    "DE": (
        ("Leon", "Lukas", "Felix", "Paul", "Jonas", "Finn", "Elias", "Maximilian", "Ben", "Niklas", "Julian", "Moritz", "Florian", "Tobias", "Lennart", "Johannes", "Fabian", "Sebastian", "Anton", "Lars"),
        ("Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker", "Schulz", "Hoffmann", "Schäfer", "Koch", "Bauer", "Richter", "Klein", "Wolf", "Schröder", "Neumann", "Braun", "Krüger"),
    ),
    "ES": (
        ("Alejandro", "Álvaro", "Daniel", "Pablo", "Hugo", "Adrián", "Diego", "Javier", "Sergio", "Marcos", "Mario", "Iván", "Rubén", "Raúl", "Carlos", "Miguel", "Andrés", "Nicolás", "Héctor", "Rodrigo"),
        ("García", "Fernández", "González", "Rodríguez", "López", "Martínez", "Sánchez", "Pérez", "Gómez", "Martín", "Jiménez", "Ruiz", "Hernández", "Díaz", "Moreno", "Muñoz", "Alonso", "Gutiérrez", "Romero", "Navarro"),
    ),
    "IT": (
        ("Lorenzo", "Matteo", "Leonardo", "Francesco", "Alessandro", "Andrea", "Gabriele", "Riccardo", "Tommaso", "Federico", "Davide", "Simone", "Niccolò", "Giovanni", "Filippo", "Marco", "Luca", "Edoardo", "Pietro", "Giulio"),
        ("Rossi", "Russo", "Ferrari", "Esposito", "Bianchi", "Romano", "Colombo", "Ricci", "Marino", "Greco", "Bruno", "Gallo", "Conti", "De Luca", "Mancini", "Costa", "Giordano", "Rizzo", "Lombardi", "Moretti"),
    ),
    "PT": (
        ("João", "Francisco", "Afonso", "Tiago", "Diogo", "Miguel", "Duarte", "Gonçalo", "Rafael", "Guilherme", "Pedro", "Tomás", "Bernardo", "André", "Nuno", "Rui"),
        ("Silva", "Santos", "Ferreira", "Pereira", "Oliveira", "Costa", "Rodrigues", "Martins", "Jesus", "Sousa", "Fernandes", "Gonçalves", "Gomes", "Lopes", "Marques", "Almeida"),
    ),
    "BR": (
        ("Gabriel", "Lucas", "Matheus", "Gustavo", "Rafael", "Felipe", "Vinícius", "Bruno", "Caio", "Thiago", "Diego", "Anderson", "Eduardo", "Leandro", "Renan", "Wesley"),
        ("da Silva", "dos Santos", "Oliveira", "Souza", "Pereira", "Costa", "Almeida", "Ferreira", "Rodrigues", "Lima", "Carvalho", "Gomes", "Ribeiro", "Barbosa", "Mendes", "Nascimento"),
    ),
    "AR": (
        ("Mateo", "Santiago", "Benjamín", "Tomás", "Joaquín", "Bautista", "Agustín", "Franco", "Lautaro", "Facundo", "Nicolás", "Ramiro", "Emiliano", "Gonzalo", "Ignacio", "Luciano"),
        ("González", "Rodríguez", "Fernández", "López", "Martínez", "Pérez", "Gómez", "Romero", "Sosa", "Torres", "Álvarez", "Ruiz", "Díaz", "Acosta", "Medina", "Herrera"),
    ),
    "NL": (
        ("Daan", "Sem", "Luuk", "Bram", "Thijs", "Lars", "Jesse", "Milan", "Finn", "Stijn", "Jasper", "Sven", "Wout", "Koen", "Jeroen", "Pieter"),
        ("de Jong", "Jansen", "de Vries", "van den Berg", "van Dijk", "Bakker", "Janssen", "Visser", "Smit", "Meijer", "de Boer", "Mulder", "de Groot", "Bos", "Vos", "van Leeuwen"),
    ),
    "SE": (
        ("Erik", "Gustav", "Oscar", "William", "Hugo", "Elias", "Viktor", "Axel", "Emil", "Isak", "Albin", "Ludvig", "Filip", "Anton", "Nils", "Johan"),
        ("Andersson", "Johansson", "Karlsson", "Nilsson", "Eriksson", "Larsson", "Olsson", "Persson", "Svensson", "Gustafsson", "Pettersson", "Jonsson", "Lindberg", "Lindgren", "Bergström", "Lundqvist"),
    ),
    "TR": (
        ("Emir", "Arda", "Kerem", "Eren", "Yusuf", "Mehmet", "Burak", "Mert", "Kaan", "Emre", "Oğuz", "Hakan", "Deniz", "Barış", "Umut", "Onur"),
        ("Yılmaz", "Kaya", "Demir", "Şahin", "Çelik", "Yıldız", "Yıldırım", "Öztürk", "Aydın", "Arslan", "Doğan", "Kılıç", "Aslan", "Çetin", "Kara", "Koç"),
    ),
    "BG": (
        ("Ivan", "Georgi", "Dimitar", "Nikolay", "Petar", "Aleksandar", "Stefan", "Martin", "Viktor", "Daniel", "Todor", "Stoyan", "Borislav", "Krasimir", "Radoslav", "Vasil"),
        ("Ivanov", "Georgiev", "Dimitrov", "Petrov", "Nikolov", "Stoyanov", "Todorov", "Kolev", "Hristov", "Angelov", "Vasilev", "Stefanov", "Iliev", "Marinov", "Popov", "Radev"),
    ),
    "JP": (
        ("Satō", "Suzuki", "Takahashi", "Tanaka", "Watanabe", "Itō", "Yamamoto", "Nakamura", "Kobayashi", "Katō", "Yoshida", "Yamada", "Sasaki", "Yamaguchi", "Matsumoto", "Inoue"),
        ("Haruto", "Yūto", "Sōta", "Ren", "Kaito", "Takumi", "Daiki", "Ryō", "Shōta", "Kenta", "Naoki", "Hiroki", "Ryōta", "Yūki", "Hayato", "Itsuki"),
    ),
    "KR": (
        ("Kim", "Lee", "Park", "Choi", "Jung", "Kang", "Cho", "Yoon", "Jang", "Lim", "Han", "Oh", "Seo", "Shin", "Kwon", "Hwang"),
        ("Min-jun", "Seo-jun", "Do-yun", "Ji-ho", "Jun-seo", "Hyun-woo", "Ji-hun", "Tae-yang", "Seung-hyun", "Dong-hyun", "Sung-min", "Jae-won", "Woo-jin", "Min-seok", "Seung-woo", "Jin-woo"),
    ),
}


def generate_identity(nationality=None):
    nationality = nationality or random.choice(tuple(NAME_POOLS))
    first, second = NAME_POOLS[nationality]
    return f"{random.choice(first)} {random.choice(second)}", nationality
